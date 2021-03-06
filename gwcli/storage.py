#!/usr/bin/env python

__author__ = 'pcuzner@redhat.com'

import os
import socket
import json

# FIXME - relative imports
from gwcli.node import UIGroup, UINode

from gwcli.client import Clients

# from configshell_fb import ExecutionError

from gwcli.utils import (human_size, readcontents,
                         GatewayAPIError, GatewayLIOError,
                         this_host, get_other_gateways)

from requests import delete, put, get, ConnectionError
from ceph_iscsi_config.utils import valid_size, convert_2_bytes

import ceph_iscsi_config.settings as settings

# FIXME - this ignores the warning issued when verify=False is used
from requests.packages import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# def display_msg(msg_type='info', msg_text=''):
#
#     prefix = {'error': '--> ',
#               'info': '- '}
#
#     if '{' in msg_text:
#         # assume json, so load it and use the 1st variable defined as
#         # the message to print
#         msg_js = json.loads(msg_text)
#         key = msg_js.keys()[0]
#         msg = msg_js[key]
#     else:
#         msg = msg_text
#
#     print("{}{}".format(prefix[msg_type],
#                         msg))



class Disks(UIGroup):

    help_intro = '''
                 The disks section provides a summary of the rbd images that
                 have been defined and added to the gateway nodes. Each disk
                 listed will provide a view of it's capacity, and you can use
                 the 'info' subcommand to retrieve lower level information
                 about the rbd image.

                 The capacity shown against each disk is the logical size of
                 the rbd image, not the physical space the image is consuming
                 within rados.

                 '''

    def __init__(self, parent):
        UIGroup.__init__(self, 'disks', parent)
        self.disk_info = {}
        self.logger = self.parent.logger

    def refresh(self, disk_info):
        self.disk_info = disk_info
        # Load the disk configuration
        for image_id in disk_info:
            image_config = disk_info[image_id]
            Disk(self, image_id, image_config)

    def reset(self):
        children = set(self.children)  # set of child objects
        for child in children:
            self.remove_child(child)

    def ui_command_create(self, pool=None, image=None, size=None):
        """
        Create a LUN and assign to the gateway.

        The create process needs the pool name, rbd image name
        and the size parameter. 'size' should be a numeric suffixed
        by either M, G or T (representing the allocation unit)
        """
        # NB the text above is shown on a help create request in the CLI

        if not self._valid_request(pool, image, size):
            return

        # get pool, image, and size ; use this host as the creator
        local_gw = this_host()
        disk_key = "{}.{}".format(pool, image)

        other_gateways = get_other_gateways(self.parent.target.children)
        if len(other_gateways) < 1:
            self.logger.error("At least 2 gateways must be defined before disks can be added")
            return

        self.logger.debug("Creating/mapping disk {}/{}".format(pool,
                                                               image))

        # make call to local api server first!
        disk_api = '{}://127.0.0.1:{}/api/disk/{}'.format(self.http_mode,
                                                          settings.config.api_port,
                                                          disk_key)

        api_vars = {'pool': pool, 'size': size.upper(), 'owner': local_gw, 'mode': 'create'}

        self.logger.debug("Processing local LIO instance")
        response = put(disk_api,
                       data=api_vars,
                       auth=(settings.config.api_user, settings.config.api_password),
                       verify=settings.config.api_ssl_verify)

        if response.status_code == 200:
            # rbd create and map successful, so request it's details and add
            # to the gwcli
            self.logger.debug("- LUN is ready on local")
            response = get(disk_api,
                           auth=(settings.config.api_user, settings.config.api_password),
                           verify=settings.config.api_ssl_verify)

            if response.status_code == 200:
                image_config = response.json()
                Disk(self, disk_key, image_config)

                self.logger.debug("Processing other gateways")
                for gw in other_gateways:
                    disk_api = '{}://{}:{}/api/disk/{}'.format(self.http_mode,
                                                               gw,
                                                               settings.config.api_port,
                                                               disk_key)

                    response = put(disk_api,
                                   data=api_vars,
                                   auth=(settings.config.api_user, settings.config.api_password),
                                   verify=settings.config.api_ssl_verify)

                    if response.status_code == 200:
                        self.logger.debug("- LUN is ready on {}".format(gw))
                    else:
                        raise GatewayAPIError(response.text)

        else:
            raise GatewayLIOError("- Error defining the rbd image to the local gateway")

        ceph_pools = self.parent.ceph.pools
        ceph_pools.refresh()

        self.logger.info('ok')


    def find_hosts(self):
        hosts = []

        tgt_group = self.parent.target.children
        for tgt in tgt_group:
            for tgt_child in tgt.children:
                if isinstance(tgt_child, Clients):
                    hosts += list(tgt_child.children)

        return hosts

    def disk_in_use(self, image_id):
        """
        determine if a given disk image is mapped to any of the defined clients
        @param: image_id ... rbd image name (<pool>.<image> format)
        :return: either an empty list or a list of clients using the disk image
        """
        disk_users = []

        client_list = self.find_hosts()
        for client in client_list:
            client_disks = [mlun.rbd_name for mlun in client.children]
            if image_id in client_disks:
                disk_users.append(client.name)

        return disk_users

    def ui_command_delete(self, image_id):
        """
        Delete a given rbd image from the configuration and ceph. This is a
        destructive action that could lead to data loss, so please ensure
        the rbd image is correct!

        > delete <rbd_image_name>

        Also note that the delete process is a synchronous task, so the larger
        the rbd image is, the longer the delete will take to run.

        """

        # 1st does the image id given exist?
        rbd_list = [disk.name for disk in self.children]
        if image_id not in rbd_list:
            self.logger.error("- the disk '{}' does not exist in this configuration".format(image_id))
            return

        # Although the LUN class will check that the lun is unallocated before attempting
        # a delete, it seems cleaner and more responsive to check through the object model
        # here before sending a delete request

        disk_users = self.disk_in_use(image_id)
        if disk_users:
            self.logger.error("- Unable to delete '{}', it is currently allocated to:".format(image_id))

            # error_str = "- Unable to delete '{}', it is currently allocated to:\n".format(image_id)
            for client in disk_users:
                self.logger.error("  - {}".format(client))
            return

        self.logger.debug("Deleting rbd {}".format(image_id))

        local_gw = this_host()
        other_gateways = get_other_gateways(self.parent.target.children)

        api_vars = {'purge_host': local_gw}
        # process other gateways first
        for gw_name in other_gateways:
            disk_api = '{}://{}:{}/api/disk/{}'.format(self.http_mode,
                                                       gw_name,
                                                       settings.config.api_port,
                                                       image_id)

            self.logger.debug("- removing '{}' from {}".format(image_id,
                                                               gw_name))
            response = delete(disk_api,
                              data=api_vars,
                              auth=(settings.config.api_user, settings.config.api_password),
                              verify=settings.config.api_ssl_verify)

            if response.status_code == 200:
                pass
            elif response.status_code == 400:
                # 400 means the rbd is still allocated to a client
                msg = json.loads(response.text)['message']
                self.logger.error(msg)
                return
            else:
                # delete failed - don't know why, pass the error to the
                # admin and abort
                raise GatewayAPIError(response.text)


        # at this point the remote gateways are cleaned up, now perform the
        # purge on the local host which will also purge the rbd
        disk_api = '{}://127.0.0.1:{}/api/disk/{}'.format(self.http_mode,
                                                          settings.config.api_port,
                                                          image_id)

        self.logger.debug("- removing '{}' from the local machine".format(image_id))

        response = delete(disk_api,
                          data=api_vars,
                          auth=(settings.config.api_user, settings.config.api_password),
                          verify=settings.config.api_ssl_verify)

        if response.status_code == 200:
            self.logger.debug("- rbd removed")
            disk_object = [disk for disk in self.children if disk.name == image_id][0]
            self.remove_child(disk_object)
        else:
            raise GatewayLIOError("--> Failed to remove the device from the local machine")

        ceph_pools = self.parent.ceph.pools
        ceph_pools.refresh()

        self.logger.info('ok')

    def _valid_request(self, pool, image, size):
        """
        Validate the parameters of a create request
        :param pool: rados pool name
        :param image: rbd image name
        :param size: size of the rbd (unit suffixed e.g. 20G)
        :return: boolean, indicating whether the parameters may be used or not
        """
        state = True
        discovered_pools = [rados_pool.name for rados_pool in self.parent.ceph.pools.children]
        existing_rbds = self.disk_info.keys()

        storage_key = "{}.{}".format(pool, image)
        if not size:
            self.logger.error("Size parameter is missing")
            state = False
        elif not valid_size(size):
            self.logger.error("Size is invalid")
            state = False
        elif pool not in discovered_pools:
            self.logger.error("pool name is invalid")
            state = False
        elif storage_key in existing_rbds:
            self.logger.error("image of that name already defined")
            state = False

        return state

    def summary(self):
        total_bytes = 0
        for disk in self.children:
            total_bytes += disk.size
        return '{}, Disks: {}'.format(human_size(total_bytes), len(self.children)), None


class Disk(UINode):

    display_attributes = ["image", "pool", "wwn", "size_h", "size", "features", "owner", "dm_device"]

    def __init__(self, parent, image_id, image_config):
        """
        Create a disk entry under the Disks subtree
        :param parent: parent object (instance of the Disks class)
        :param image_id: key used in the config object for this rbd image (pool.image_name) - str
        :param image_config: meta data for this image
        :return:
        """
        self.pool, self.rbd_image = image_id.split('.', 1)

        UINode.__init__(self, image_id, parent)
        self.image_id = image_id
        self.logger = self.parent.logger
        self.size = 0
        self.size_h = ''

        disk_map = self.parent.disk_info
        if image_id not in disk_map:
            disk_map[image_id] = {}

        # set the remaining attributes based on the fields in the dict
        for k, v in image_config.iteritems():
            disk_map[image_id][k] = v
            self.__setattr__(k, v)

        # Size/features are not stored in the config, since it can be changed by a number
        # of different means, so we get them dynamically on the CLI host
        self.get_meta_data()

    def summary(self):
        msg = [self.dm_device, "({})".format(self.size_h)]

        return " ".join(msg), True

    def get_meta_data(self):
        # image_path is a symlink to the actual /dev/rbdX file
        image_path = "/dev/rbd/{}/{}".format(self.pool, self.rbd_image)
        dev_id = os.path.realpath(image_path)[8:]
        rbd_path = "/sys/devices/rbd/{}".format(dev_id)
        self.features = readcontents(os.path.join(rbd_path, 'features'))
        self.size = int(readcontents(os.path.join(rbd_path, 'size')))
        self.size_h = human_size(self.size)

        # update the parent's disk info map
        disk_map = self.parent.disk_info

        disk_map[self.image_id]['size'] = self.size
        disk_map[self.image_id]['size_h'] = self.size_h

    def ui_command_resize(self, size=None):
        """
        The resize command allows you to increase the size of an
        existing rbd image. Attempting to decrease the size of an
        rbd will be ignored.

        size: new size including unit suffix e.g. 300G

        """

        # resize is actually managed by the same lun and api endpoint as
        # create so this logic is very similar to a 'create' request

        if not size:
            self.logger.error("Specify a size value (current size is {})".format(self.size_h))
            return

        size_rqst = size.upper()
        if not valid_size(size_rqst):
            self.logger.error("Size parameter value is not valid syntax (must be of the form 100G, or 1T)")
            return

        new_size = convert_2_bytes(size_rqst)
        if self.size >= new_size:
            # current size is larger, so nothing to do
            self.logger.error("New size isn't larger than the current image size, ignoring request")
            return

        # At this point the size request needs to be honoured
        self.logger.debug("Resizing {} to {}".format(self.image_id,
                                                     size_rqst))

        local_gw = this_host()
        other_gateways = get_other_gateways(self.parent.parent.target.children)

        # make call to local api server first!
        disk_api = '{}://127.0.0.1:{}/api/disk/{}'.format(self.http_mode,
                                                          settings.config.api_port,
                                                          self.image_id)

        api_vars = {'pool': self.pool, 'size': size_rqst, 'owner': local_gw, 'mode': 'resize'}

        self.logger.debug("Processing local LIO instance")
        response = put(disk_api,
                       data=api_vars,
                       auth=(settings.config.api_user, settings.config.api_password),
                       verify=settings.config.api_ssl_verify)

        if response.status_code == 200:
            # rbd resize request successful, so update the local information
            self.logger.debug("- LUN resize complete")
            self.get_meta_data()

            self.logger.debug("Processing other gateways")
            for gw in other_gateways:
                disk_api = '{}://{}:{}/api/disk/{}'.format(self.http_mode,
                                                           gw,
                                                           settings.config.api_port,
                                                           self.image_id)

                response = put(disk_api,
                               data=api_vars,
                               auth=(settings.config.api_user, settings.config.api_password),
                               verify=settings.config.api_ssl_verify)

                if response.status_code == 200:
                    self.logger.debug("- LUN resize registered on {}".format(gw))
                else:
                    raise GatewayAPIError(response.text)

        else:
            raise GatewayAPIError(response.text)

        self.logger.info('ok')
