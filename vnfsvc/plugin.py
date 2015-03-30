# Copyright 2014 Tata Consultancy Services Ltd.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json
import os
import uuid
import shutil
import six
import eventlet
import pexpect
import tarfile
import time
import sys 
import re
import yaml
import ast
import subprocess
import traceback

from collections import OrderedDict
from distutils import dir_util
from netaddr import IPAddress, IPNetwork
from oslo.config import cfg

from vnfsvc import constants
from vnfsvc import manager
from vnfsvc import constants as vm_constants
from vnfsvc import config
from vnfsvc import context
from vnfsvc import nsdmanager

from vnfsvc.api.v2 import attributes
from vnfsvc.api.v2 import vnf
from vnfsvc.db.vnf import vnf_db
from vnfsvc import vnffg

from vnfsvc.openstack.common.gettextutils import _
from vnfsvc.openstack.common import excutils
from vnfsvc.openstack.common import log as logging
from vnfsvc.openstack.common import importutils

from vnfsvc.client import client

from vnfsvc.common import driver_manager
from vnfsvc.common import exceptions
from vnfsvc.common import rpc as v_rpc
from vnfsvc.common import topics
#from vnfsvc.common import utils
from vnfsvc.agent.linux import utils

from vnfsvc.common.yaml.nsdparser import NetworkParser
from vnfsvc.common.yaml.vnfdparser import VNFParser

LOG = logging.getLogger(__name__)


class VNFPlugin(vnf_db.NetworkServicePluginDb):
    """VNFPlugin which provide support to OpenVNF framework"""

    #register vnf driver 
    OPTS = [
        cfg.MultiStrOpt(
            'vnf_driver', default=[],
            help=_('Hosting  drivers for vnf will use')),
        cfg.StrOpt(
            'templates', default='',
            help=_('Path to service templates')),
        cfg.StrOpt(
            'vnfmanager', default='',
            help=_('Path to VNFManager')),
        cfg.StrOpt(
            'compute_hostname', default='',
            help=_('Compute Hostname')),
        cfg.StrOpt(
            'compute_user', default='',
            help=_('User name')),
        cfg.StrOpt(
            'vnfm_home_dir', default='',
            help=_('vnf_home_dir')),
        cfg.StrOpt(
            'compute_home_user', default='',
            help=_('compute_home_user')),
        #cfg.StrOpt(
        #    'puppet_master_image_id', default='',
        #    help=_('puppet_master_image_id')),
        cfg.StrOpt(
            'ssh_pwd', default='',
            help=_('ssh_pwd')),
        cfg.StrOpt(
            'ovs_bridge', default='br-int',
            help=_('ovs_bridge')),
        cfg.StrOpt(
            'neutron_rootwrap', default='',
            help=_('path to neutron rootwrap')),
        cfg.StrOpt(
            'neutron_rootwrapconf', default='',
            help=_('path to neutron rootwrap conf')),
        cfg.StrOpt(
            'vnfmconf', default='local',
            help=_('VNFManager Configuaration')),
    ]
    cfg.CONF.register_opts(OPTS, 'vnf')
    conf = cfg.CONF

    def __init__(self):
        super(VNFPlugin, self).__init__()
        self.novaclient = client.NovaClient()
        self.glanceclient = client.GlanceClient()
        self.neutronclient = client.NeutronClient()
        self._pool = eventlet.GreenPool()
        self.conf = cfg.CONF
        self.is_manager_invoked =  False
        self.ns_dict = dict()

        config.register_root_helper(self.conf)
        self.root_helper = config.get_root_helper(self.conf)
        self.agent_mapping = dict()

        self.endpoints = [VNFManagerCallbacks(self)]
        self.conn = v_rpc.create_connection(new=True)
        self.conn.create_consumer(
            topics.PLUGIN, self.endpoints, fanout=False)

        self.conn.consume_in_threads()


    def spawn_n(self, function, *args, **kwargs):
        self._pool.spawn_n(function, *args, **kwargs)


    def _get_networks(self, ns_info):
        return ns_info['attributes']['networks']

    def _get_router(self, ns_info):
        return ns_info['attributes']['router']

    def _get_subnets(self, ns_info):
        return ns_info['attributes']['subnets']

    def _get_qos(self, ns_info):
        return ns_info['quality_of_service']

    def _get_name(self, ns_info):
        return ns_info['name']

    def _ns_dict_init(self, service, nsd_id):
        ns_info = {}
        ns_info[nsd_id] = service['service']
        self.ns_dict[nsd_id]= {}
        self.ns_dict[nsd_id]['vnfds'] = {}
        self.ns_dict[nsd_id]['instances'] = {}
        self.ns_dict[nsd_id]['created'] = []
        self.ns_dict[nsd_id]['image_list'] = []
        self.ns_dict[nsd_id]['flavor_list'] = []
        self.ns_dict[nsd_id]['puppet'] = ''
        self.ns_dict[nsd_id]['conf_generated'] = []
        self.ns_dict[nsd_id]['vnfmanager_uuid'] = str(uuid.uuid4())
        self.ns_dict[nsd_id]['acknowledge_list'] = dict()
        self.ns_dict[nsd_id]['deployed_vdus'] = list()
        self.ns_dict[nsd_id]['vnfm_dir'] =  self.conf.state_path+'/'+ \
                                    self.ns_dict[nsd_id]['vnfmanager_uuid']

        if not os.path.exists(self.ns_dict[nsd_id]['vnfm_dir']):
            os.makedirs(self.ns_dict[nsd_id]['vnfm_dir'])
        self.ns_dict[nsd_id]['service_name'] = self._get_name(ns_info[nsd_id])
        self.ns_dict[nsd_id]['networks'] = self._get_networks(ns_info[nsd_id])
        self.ns_dict[nsd_id]['router'] = self._get_router(ns_info[nsd_id])
        self.ns_dict[nsd_id]['subnets'] = self._get_subnets(ns_info[nsd_id])
        self.ns_dict[nsd_id]['qos'] = self._get_qos(ns_info[nsd_id])
        
        self.ns_dict[nsd_id]['templates_json'] = json.load(
                                       open(self.conf.vnf.templates, 'r'))

        self.ns_dict[nsd_id]['nsd_template'] = yaml.load(open(
                          self.ns_dict[nsd_id]['templates_json']['nsd']\
                          [self.ns_dict[nsd_id]['service_name']], 'r'))['nsd']


    def create_service(self, context, service):
        nsd_id = str(uuid.uuid4())
        nsd_dict = {}
        nsd_dict['id'] = nsd_id
        nsd_dict['check'] = ''

        try:
            self._ns_dict_init(service, nsd_id)

            self.ns_dict[nsd_id]['nsd_template'] = NetworkParser(
                                self.ns_dict[nsd_id]['nsd_template']).parse(
                                             self.ns_dict[nsd_id]['qos'],
                                             self.ns_dict[nsd_id]['networks'] ,
                                             self.ns_dict[nsd_id]['router'],
                                             self.ns_dict[nsd_id]['subnets'])

            self.ns_dict[nsd_id]['nsd_template']['router'] = {}
            self.ns_dict[nsd_id]['nsd_template'] = nsdmanager.Configuration(
                               self.ns_dict[nsd_id]['nsd_template']).preconfigure()

            for vnfd in self.ns_dict[nsd_id]['nsd_template']['vnfds']:
                vnfd_template = yaml.load(open(
                               self.ns_dict[nsd_id]['templates_json']['vnfd'][vnfd],
                               'r'))
                self.ns_dict[nsd_id]['vnfds'][vnfd] = dict()
                self.ns_dict[nsd_id]['vnfds'][vnfd]['template'] = vnfd_template
                self.ns_dict[nsd_id]['vnfds'][vnfd] = VNFParser(
                               self.ns_dict[nsd_id]['vnfds'][vnfd],
                               self.ns_dict[nsd_id]['qos'],
                               self.ns_dict[nsd_id]['nsd_template']['vnfds'][vnfd],
                               vnfd,
                               self.ns_dict[nsd_id]['nsd_template']).parse()
                self.ns_dict[nsd_id]['vnfds'][vnfd]['vnf_id'] = str(uuid.uuid4())

            db_dict = {
                'id': nsd_id,
                'nsd': self.ns_dict[nsd_id]['nsd_template'],
                'vnfds': self.ns_dict[nsd_id]['vnfds'],
                'networks': self.ns_dict[nsd_id]['networks'],
                'subnets': self.ns_dict[nsd_id]['subnets'],
                'vnfm_id': self.ns_dict[nsd_id]['vnfmanager_uuid'],
                'service': service['service'],
                'status': 'PENDING'
            }
            #Create DB Entry for the new service
            nsdb_dict = self.create_service_model(context, **db_dict)

            #Launch VNFDs
            self._create_vnfds(context,nsd_id)

            #TODO : (tcs) Need to enhance computation of forwarding graph
            vnffg.ForwardingGraph(self.ns_dict[nsd_id]['nsd_template'],
                               self.ns_dict[nsd_id]['vnfds']
                                 ).configure_forwarding_graph()

            self.update_nsd_status(context, nsd_id, 'ACTIVE')
        except Exception as e:
            print 'AN EXCEPTION HAS OCCURED'
            traceback.print_exc(file=sys.stdout)
            nsd_dict['check'] = e
            return nsd_dict
        return nsdb_dict


    def delete_service(self, context, service):
        nsd_id = service
        service_db_dict = self.delete_service_model(context, service)
        try:
            puppet_master_image = self.ns_dict[nsd_id]['nsd_template']['puppet-master']['instance_id']
            self.novaclient.delete(puppet_master_image)
        except Exception as e:
            pass
        if service_db_dict is not None:
            self._delete_vtap_and_vnfm(service_db_dict)
            try:
                self._delete_flavor_and_image(service_db_dict,service)
                self._delete_instances(service_db_dict)
                time.sleep(15)
                self._delete_router_interfaces(service_db_dict)
                self._delete_router(service_db_dict)
                self._delete_ports(service_db_dict)
                self._delete_networks(service_db_dict)
                self.delete_db_dict(context,service)
            except Exception:
                raise
        else:
            return

    def _delete_vtap_and_vnfm(self, service_db_dict):
        try:
            vnfm_id = service_db_dict['service_db'][0].vnfm_id
            homedir = self.conf.state_path
            with open(homedir+"/"+vnfm_id+"/ovs.sh","r") as f:
                data = f.readlines()
            subprocess.call(["sudo","ovs-vsctl","del-port",data[2].split(" ")[2]])
            subprocess.call(["sudo","pkill","-9","vnf-manager"])
        except Exception as e:
            pass

    def _delete_instances(self, service_db_dict):
        instances = []
        for instance in range(len(service_db_dict['instances'])):
            instances.append(service_db_dict['instances'][instance][0].__dict__['instances'].split(','))
        for inst in instances:
            for prop in range(len(inst)):
                try:
                    self.novaclient.delete(inst[prop])
                except Exception as e:
                     pass
        
    def _delete_flavor_and_image(self, service_db_dict, service):
        nsd_id = service
        try:
            for image_id in self.ns_dict[nsd_id]['image_list']:
                try:
                    self.glanceclient.delete_image(image_id)
                except:
                    pass
            for flavor_id in self.ns_dict[nsd_id]['flavor_list']:
                try:
                    self.novaclient.delete_flavor(flavor_id)
                except:
                    pass
        except Exception as e:
            pass

    def _delete_router_interfaces(self, service_db_dict):
        fixed_ips = []
        subnet_ids = []
        body = {}
        router_id = ast.literal_eval(service_db_dict['service_db'][0].router)['id']
        router_ports = self.neutronclient.list_router_ports(router_id)

        for r_port in range(len(router_ports['ports'])):
            fixed_ips.append(router_ports['ports'][r_port]['fixed_ips'])

        for ip in range(len(fixed_ips)):
            subnet_ids.append(fixed_ips[ip][0]['subnet_id'])

        for s_id in range(len(subnet_ids)):
            body['subnet_id']=subnet_ids[s_id]
            try:
                self.neutronclient.remove_interface_router(router_id, body)
            except Exception as e:
                pass


    def _delete_router(self, service_db_dict):
        router_id = ast.literal_eval(service_db_dict['service_db'][0].router)['id']
        try:
            self.neutronclient.delete_router(router_id)
        except Exception as e:
             pass

    
    def _delete_ports(self, service_db_dict):
        net_ids = ast.literal_eval(service_db_dict['service_db'][0].networks).values()
        port_list=self.neutronclient.list_ports()

        for port in range(len(port_list['ports'])):
            if port_list['ports'][port]['network_id'] in net_ids:
               port_id = port_list['ports'][port]['id']
               try:
                   self.neutronclient.delete_port(port_id)
               except Exception as e:
                   pass

    
    def _delete_networks(self, service_db_dict):
        net_ids = ast.literal_eval(service_db_dict['service_db'][0].networks).values()
        for net in range(len(net_ids)):
            try:
                self.neutronclient.delete_network(net_ids[net])
            except Exception as e:
                pass


    def remove_keys(self, temp_vdus, key_list):
        for vdu in key_list:
            del temp_vdus[vdu]
        return temp_vdus


    def create_dependency_list(self, nsd_id):
        """ Represents the VDU dependencies for a Network Service
            in a tree structure  """
        temp_vdus = self.ns_dict[nsd_id]['nsd_template']['vdus'].copy()
        self.ns_dict[nsd_id]['dependency_list'] = list()
        #Add Independent tuples  to the list
        temp_list = list()
        for vdu in temp_vdus.keys():
            if 'dependency' not in temp_vdus[vdu].keys():
                temp_list.append(vdu)
        temp_vdus = self.remove_keys(temp_vdus, temp_list)
        self.ns_dict[nsd_id]['dependency_list'].append(set(temp_list))
        #Add Next Level vdus
        while len(temp_vdus.keys())>0:
            temp_list = list()
            for vdu in temp_vdus.keys():
                temp_dependencies = temp_vdus[vdu]['dependency']
                dependency_satisfied = True
                for temp_vdu in temp_dependencies:
                    if temp_vdu in temp_vdus.keys():
                       dependency_satisfied = False
                       break
                if dependency_satisfied:
                    temp_list.append(vdu)
            temp_vdus = self.remove_keys(temp_vdus, temp_list)
            self.ns_dict[nsd_id]['dependency_list'].append(set(temp_list))


    def _get_vnfds_no_dependency(self ,nsd_id):
        """ Returns all the vnfds which don't have dependency """
        temp_vnfds = list()
        for vnfd in self.ns_dict[nsd_id]['nsd_template']['vnfds']:
            for vdu in self.ns_dict[nsd_id]['nsd_template']['vnfds'][vnfd]:
                if 'dependency' not in self.ns_dict[nsd_id]['nsd_template']\
                                       ['vdus'][vnfd+':'+vdu].keys():
                    temp_vnfds.append(vnfd+':'+vdu)
        return temp_vnfds

    
    def _create_flavor(self, vnfd, vdu, nsd_id):
        """ Create a openstack flavor based on vnfd flavor """
        flavor_dict = VNFParser().get_flavor_dict(
                             self.ns_dict[nsd_id]['vnfds'][vnfd]['vdus'][vdu])
        flavor_dict['name'] = vnfd+'_'+vdu+flavor_dict['name']
        return self.novaclient.create_flavor(**flavor_dict)


    def _create_vnfds(self, context, nsd_id):
        self.create_dependency_list(nsd_id)

        """ Deploy independent VNF/VNF'S """
        independent_vdus = self.ns_dict[nsd_id]['dependency_list'][0]
        for vnfd in independent_vdus:
            self._launch_vnfds(vnfd, context, nsd_id)
        self._invoke_vnf_manager(context, nsd_id)


    def wait_for_acknowledgment(self, vdus, nsd_id):
        acknowledged = False
        while not acknowledged:
            vdu_count = 0
            for vdu in vdus:
                if vdu in self.ns_dict[nsd_id]['created']:
                    vdu_count = vdu_count + 1
            if vdu_count == len(vdus):
                acknowledged = True
            else:
                time.sleep(5)


    def _resolve_dependency(self, context, nsd_id):
       self.wait_for_acknowledgment(self.ns_dict[nsd_id]['dependency_list'][0],
                                    nsd_id)
       for index in range(1, len(self.ns_dict[nsd_id]['dependency_list'])):
           vdus = self.ns_dict[nsd_id]['dependency_list'][index]
           for vdu in vdus:
               self._launch_vnfds(vdu, context, nsd_id)
           conf =  self._generate_vnfm_conf(nsd_id)
           for vdu in vdus:
               self.ns_dict[nsd_id]['conf_generated'].append(vdu)
           self.agent_mapping[self.ns_dict[nsd_id]['vnfmanager_uuid']].\
                  configure_vdus(context, conf=conf)
           self.wait_for_acknowledgment(
                  self.ns_dict[nsd_id]['dependency_list'][index], nsd_id)


    def _get_vm_details(self, vnfd_name, vdu_name, nsd_id):
        flavor = self._create_flavor(vnfd_name, vdu_name, nsd_id)
        self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus'][vdu_name]\
                    ['new_flavor'] = flavor.id
        self.ns_dict[nsd_id]['flavor_list'].append(flavor.id)
        name = vnfd_name.lower()+'-'+vdu_name.lower()
        vm_details = VNFParser().get_boot_details(
                                 self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                                 ['vdus'][vdu_name])
        vm_details['name'] = name
        vm_details['flavor'] = flavor
        return vm_details         


    def _get_vm_image_details(self, vnfd_name, vdu_name, nsd_id):
        image_details = self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus']\
                                    [vdu_name]['vm_details']['image_details']
        if 'image-id' in image_details.keys():
            image = self.glanceclient.get_image(image_details['image-id'])
        else:
            image_details['data'] = open(image_details['image'], 'rb')
            image = self.glanceclient.create_image(**image_details)
            while image.status!='active':
                time.sleep(5)
                image = self.glanceclient.get_image(image.id)
            self.ns_dict[nsd_id]['image_list'].append(image.id)
        self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus'][vdu_name]\
                    ['new_img'] = image.id
        for key in image_details.keys():
            if key not in ['username', 'password']:
                del image_details[key]
        return image    


    def _get_vm_network_details(self, vnfd_name, vdu_name, nsd_id):
        nics = []
        nw_ifaces = self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus']\
                                [vdu_name]['vm_details']['network_interfaces']
        for iface in nw_ifaces:
            if 'port_id' in nw_ifaces[iface].keys():
               nics.append({"subnet-id":nw_ifaces[iface]['subnet-id'],
                    "net-id": nw_ifaces[iface]['net-id'],
                    "port-id": nw_ifaces[iface]['port_id']})
            else:
                nics.append({"subnet-id": nw_ifaces[iface]['subnet-id'],
                             "net-id": nw_ifaces[iface]['net-id']})

        mgmt_id =  self.ns_dict[nsd_id]['networks']['mgmt-if']
        for iface in range(len(nics)):
            if nics[iface]['net-id'] == mgmt_id:
               iface_net = nics[iface]
               del nics[iface]
               nics.insert(0, iface_net)
               break
        return nics


    def _launch_vnfds(self, vnfd, context, nsd_id):
        vnfd_name, vdu_name = vnfd.split(':')[0],vnfd.split(':')[1]
        vm_details = self._get_vm_details(vnfd_name, vdu_name, nsd_id)
        vm_details['image_created'] = self._get_vm_image_details(vnfd_name,
                                                                 vdu_name,
                                                                 nsd_id)
        vm_details['nics'] = self._get_vm_network_details(vnfd_name,
                                                          vdu_name,
                                                          nsd_id)

        vm_details['userdata'] = self.set_default_userdata(vm_details,
                                                           nsd_id)
        self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus'][vdu_name]\
                    ['vm_details']['userdata'] = vm_details['userdata']

        if vnfd_name == 'loadbalancer':
            self.set_default_userdata_loadbalancer(vm_details,
                                                   nsd_id)

        with open(vm_details['userdata'], 'r') as ud_file:
            data = ud_file.readlines()
        data.insert(0, '#cloud-config\n')
        with open(vm_details['userdata'], 'w') as ud_file:
            ud_file.writelines(data)

        # Update flavor and image details for the vdu
        self.update_vdu_details(context,
                                self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                                            ['vdus'][vdu_name]['new_flavor'],
                                self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                                            ['vdus'][vdu_name]['new_img'],
                                self.ns_dict[nsd_id]['nsd_template']['vdus']\
                                            [vnfd_name+':'+vdu_name]['id'])

        deployed_vdus = self._boot_vdu(context, vnfd, nsd_id, **vm_details)
        if type(deployed_vdus) == type([]):
            self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus'][vdu_name]\
                        ['instances'] = deployed_vdus
        else:
            self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus'][vdu_name]\
                        ['instances'] = [deployed_vdus]
        self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus'][vdu_name]\
                        ['instance_list'] = []

        # Create dictionary with vdu and it's corresponding nova instance ID details
        self._populate_instances_id(vnfd_name, vdu_name, nsd_id)

        for instance in self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                             ['vdus'][vdu_name]['instances']:
            name = instance.name
            self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus'][vdu_name]\
                        ['instance_list'].append(name)

        self.ns_dict[nsd_id]['deployed_vdus'].append(vnfd)
        self._set_mgmt_ip(vnfd_name, vdu_name, nsd_id)
        self._set_instance_ip(vnfd_name, vdu_name, nsd_id)


    def set_default_userdata(self, vm_details, nsd_id):
        #TODO: (tcs) Need to enhance regarding puppet installation
        temp_dict = {'runcmd':[], 'manage_etc_hosts': 'localhost'}
        temp_dict['runcmd'].append('dhclient eth1')
        if 'cfg_engine' in self.ns_dict[nsd_id]['nsd_template']\
                                       ['preconfigure'].keys() and \
                           self.ns_dict[nsd_id]['nsd_template']\
                                       ['preconfigure']['cfg_engine'] != "":
            puppet_master_ip = self.ns_dict[nsd_id]['nsd_template']\
                                           ['puppet-master']['master-ip']
            puppet_master_hostname = self.ns_dict[nsd_id]['nsd_template']\
                                          ['puppet-master']['master-hostname']
            puppet_master_instance_id = self.ns_dict[nsd_id]['nsd_template']\
                                          ['puppet-master']['instance_id']
            self.ns_dict[nsd_id]['puppet'] = puppet_master_instance_id
            temp_dict['runcmd'].append('sudo echo '+ puppet_master_ip + \
                                       ' ' + puppet_master_hostname + \
                                       ' >> /etc/hosts')

        if 'userdata' in vm_details.keys() and vm_details['userdata'] != "":
            with open(vm_details['userdata'], 'r') as f:
                data = yaml.safe_load(f)
            if 'runcmd' in data.keys():
                temp_dict['runcmd'].extend(data['runcmd'])
                data['runcmd'] = temp_dict['runcmd']
            else:
                data['runcmd'] = temp_dict['runcmd']
            data['manage_etc_hosts'] = temp_dict['manage_etc_hosts']
            with open(self.ns_dict[nsd_id]['vnfm_dir']+'/userdata',
                      'w') as ud_file:
                yaml.safe_dump(data, ud_file)
        else:
          with open(self.ns_dict[nsd_id]['vnfm_dir']+ '/userdata',
                    'w') as ud_file:
              yaml.safe_dump(temp_dict, ud_file)
        return self.ns_dict[nsd_id]['vnfm_dir']+'/userdata'


    def set_default_userdata_loadbalancer(self, vm_details, nsd_id):
        nics = vm_details['nics']
        cidr = ''
        for network in nics:
            if network['net-id'] != self.ns_dict[nsd_id]['networks']['mgmt-if']:
                 subnet_id = network['subnet-id']
                 cidr = self.neutronclient.show_subnet(subnet_id)\
                                           ['subnet']['cidr']
                 break
        if  cidr != ''  and 'userdata' in vm_details.keys():
            with open(vm_details['userdata'], 'r') as f:
                data = yaml.safe_load(f)
            ip  = cidr.split('/')[0]
            ip = ip[0:-1]+'1'
            data['runcmd'].insert(1,"sudo ip route del default")
            data['runcmd'].insert(2,"sudo ip route add default via "+ ip + \
                                    " dev eth1")
            with open(vm_details['userdata'], 'w') as userdata_file:
                yaml.safe_dump(data, userdata_file)


    def _populate_instances_id(self, vnfd_name, vdu_name, nsd_id):
        self.ns_dict[nsd_id]['instances'][vnfd_name+':'+vdu_name] = []
        for instance in self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                                    ['vdus'][vdu_name]['instances']:
            self.ns_dict[nsd_id]['instances'][vnfd_name+':'+vdu_name].\
                     append(instance.id)


    def _generate_vnfm_conf(self, nsd_id):
        vnfm_dict = {}
        vnfm_dict['service'] = {}
        vnfm_dict['service']['nsd_id'] = nsd_id
        current_vnfs = [vdu for vdu in self.ns_dict[nsd_id]['deployed_vdus'] \
                        if vdu not in self.ns_dict[nsd_id]['conf_generated']]
        for vnf in current_vnfs:
            if not self.is_manager_invoked:
                vnfm_dict['service']['id'] = self.ns_dict[nsd_id]\
                                                  ['service_name']
                vnfm_dict['service']['fg'] = self.ns_dict[nsd_id]\
                                             ['nsd_template']\
                                             ['postconfigure']\
                                             ['forwarding_graphs']
            vnfd_name, vdu_name = vnf.split(':')[0],vnf.split(':')[1]
            if vnfd_name  not in vnfm_dict['service'].keys(): 
                vnfm_dict['service'][vnfd_name] = list()
            vdu_dict = {}
            vdu_dict['name'] = vdu_name
            vdu = self.ns_dict[nsd_id]['vnfds'][vnfd_name]['vdus'][vdu_name]
            for property in vdu:
                if property not in ['preconfigure', 'instances']:
                    if property == 'postconfigure':
                        vdu_dict.update(vdu['postconfigure'])
                    else:
                        vdu_dict[property] = vdu[property]
            vnfm_dict['service'][vnfd_name].append(vdu_dict)
            self.ns_dict[nsd_id]['conf_generated'].append(vnf)

        return vnfm_dict


    def _boot_vdu(self, context, vnfd, nsd_id, **vm_details):
        instance = self.novaclient.server_create(**vm_details)
        if vm_details['num_instances'] == 1:
            instance = self.novaclient.get_server(instance.id)
            self.update_vdu_instance_details(context,
                                             instance.id,
                                             self.ns_dict[nsd_id]\
                                             ['nsd_template']['vdus']\
                                             [vnfd]['id'])
            while instance.status != 'ACTIVE' or \
                   all(not instance.networks[iface] \
                   for iface in instance.networks.keys()):
                time.sleep(3)
                instance = self.novaclient.get_server(instance.id)
                if instance.status == 'ERROR':
                    self.update_nsd_status(context, nsd_id, 'ERROR')
                    raise exceptions.InstanceException()
        else:
            instances_list = instance
            instance = list()
            instances_active = 0
            temp_instance = None
            for temp_instance in instances_list:
                self.update_vdu_instance_details(context, temp_instance.id,
                                self.ns_dict[nsd_id]['nsd_template']\
                                ['vdus'][vnfd]['id'])

            while instances_active != vm_details['num_instances'] or \
                  len(instances_list) > 0:
                for inst in instances_list:
                    temp_instance = self.novaclient.get_server(inst.id)
                    if temp_instance.status == 'ACTIVE':
                        instances_active += 1
                        instances_list.remove(inst)
                        instance.append(inst)
                    elif temp_instance.status == 'ERROR':
                        self.update_nsd_status(nsd_id, 'ERROR')
                        raise exceptions.InstanceException()
                    else:
                        time.sleep(3)

        return instance
 

    def _set_instance_ip(self, vnfd_name, vdu_name, nsd_id):
        instances = self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                      ['vdus'][vdu_name]['instances']
        ninterfaces = self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                        ['vdus'][vdu_name]['vm_details']['network_interfaces']
        for interface in ninterfaces:
            subnet =self.neutronclient.show_subnet(
                           ninterfaces[interface]['subnet-id'])
            cidr = subnet['subnet']['cidr']
            ninterfaces[interface]['ips'] = self._get_ips(instances, cidr)

        
    def _get_ips(self, instances, cidr):
        ip_list = {}
        for instance in instances:
            instance_name = instance.name
            networks = instance.addresses
            for network in networks.keys():
                for i in range(len(networks[network])):
                    ip = networks[network][i]['addr']
                    if IPAddress(ip) in IPNetwork(cidr):
                       ip_list[instance_name]= ip
        return ip_list 


    def _set_mgmt_ip(self,vnfd_name, vdu_name, nsd_id):
        instances = self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                         ['vdus'][vdu_name]['instances']
        self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                    ['vdus'][vdu_name]['mgmt-ip'] = {}
        mgmt_cidr = self.ns_dict[nsd_id]['nsd_template']['mgmt-cidr']
        for instance in instances:
            networks = instance.addresses
            for network in networks.keys():
                for subnet in networks[network]:
                    ip = subnet['addr']
                    if IPAddress(ip) in IPNetwork(mgmt_cidr):
                        self.ns_dict[nsd_id]['vnfds'][vnfd_name]\
                             ['vdus'][vdu_name]['mgmt-ip'][instance.name] = ip


    def _copy_vnfmanager(self):
        src = self.conf.vnf.vnfmanager
        dest = '/tmp/vnfmanager'
        try:
            dir_util.copy_tree(src, dest)
            return dest
        except OSError as exc:
            raise


    def get_service(self, context, service, **kwargs):
        service = self.get_service_model(context, service, fields=None)
        return service


    def get_services(self,context, **kwargs):
        service=self.get_all_services(context, **kwargs)
        return service


    def _make_tar(self, vnfmanager_path):
        tar = tarfile.open(vnfmanager_path+'.tar.gz', 'w:gz')
        tar.add(vnfmanager_path)
        tar.close() 
        return vnfmanager_path+'.tar.gz'


    def _invoke_vnf_manager(self, context, nsd_id):
        """Invokes VNFManager using ansible(if multihost)"""
        vnfm_conf_dict = self._generate_vnfm_conf(nsd_id)
        with open(self.ns_dict[nsd_id]['vnfm_dir'] + '/' + \
                  self.ns_dict[nsd_id]['vnfmanager_uuid']+'.yaml', 'w') as f:
            yaml.safe_dump(vnfm_conf_dict, f)
        vnfm_conf = self.ns_dict[nsd_id]['vnfm_dir'] + '/' + \
                    self.ns_dict[nsd_id]['vnfmanager_uuid']+'.yaml'
        vnfsvc_conf = cfg.CONF.config_file[0]
        

        ovs_path,p_id = self._create_ovs_script(self.ns_dict[nsd_id]\
                                           ['nsd_template']\
                                           ['networks']['mgmt-if']['id'],
                                           nsd_id)
        vnfm_host =  self.novaclient.check_host(cfg.CONF.vnf.compute_hostname)
        if cfg.CONF.vnf.vnfmconf == "local":
            confcmd  = 'vnf-manager ' + \
                       '--config-file /etc/vnfsvc/vnfsvc.conf'\
                       ' --vnfm-conf-dir ' + \
                       self.ns_dict[nsd_id]['vnfm_dir'] + \
                       '/ --log-file ' + self.ns_dict[nsd_id]['vnfm_dir'] + \
                       '/vnfm.log --uuid ' + \
                       self.ns_dict[nsd_id]['vnfmanager_uuid']

            ovscmd =  'sudo sh '+ self.ns_dict[nsd_id]['vnfm_dir'] + '/ovs.sh'
            proc = subprocess.Popen(ovscmd, shell=True)
            proc2 = subprocess.Popen(confcmd.split(),
                                     stderr=open('/dev/null', 'w'),
                                     stdout=open('/dev/null', 'w'))

        elif cfg.CONF.vnf.vnfmconf == "ansible":
            with open(self.ns_dict[nsd_id]['vnfm_dir']+'/hosts', 'w') as hosts_file:
                 hosts_file.write("[server]\n%s\n"%(vnfm_host.host_ip))
            vnfm_home_dir =  '{{ ansible_env["HOME"] }}/.vnfm/' +self.ns_dict[nsd_id]\
                                                         ['vnfmanager_uuid']

            ansible_dict=[{'tasks': [
                            {'ignore_errors': True, 'shell': 'mkdir -p '+\
                          vnfm_home_dir},
                            {'copy': 'src='+ vnfsvc_conf +' dest='+\
                          vnfm_home_dir+'/vnfsvc.conf'},
                            {'copy': 'src='+ vnfm_conf + ' dest=' + \
                          vnfm_home_dir + '/' + \
                          self.ns_dict[nsd_id]['vnfmanager_uuid'] + \
                          '.yaml'},
                            {'async': 1000000, 'poll': 0,
                          'command': 'vnf-manager --config-file '+ \
                          vnfm_home_dir + '/vnfsvc.conf --vnfm-conf-dir '+ \
                          vnfm_home_dir + '/ --log-file ' + vnfm_home_dir + \
                          '/vnfm.log --uuid %s'
                          % self.ns_dict[nsd_id]['vnfmanager_uuid'],
                          'name': 'run manager'},
                            {'copy': 'src='+ ovs_path + ' dest=' + \
                          vnfm_home_dir + '/ovs.sh'},
                            {'ignore_errors': True, 'shell': 'sh '+ \
                          vnfm_home_dir + '/ovs.sh', 'register': 'result1'},
                            {'debug': 'var=result1.stdout_lines'},
                        ], 'hosts': 'server', 'remote_user': \
                          cfg.CONF.vnf.compute_user}]

         
            with open(self.ns_dict[nsd_id]['vnfm_dir'] +\
                      '/vnfmanager-playbook.yaml', 'w') as yaml_file:
                yaml_file.write( yaml.dump(ansible_dict, 
                                           default_flow_style=False))
            LOG.debug(_('----- Launching VNFManager -----'))

            child = pexpect.spawn('ansible-playbook ' +\
                                  self.ns_dict[nsd_id]['vnfm_dir'] +\
                                  '/vnfmanager-playbook.yaml -i ' +\
                                  self.ns_dict[nsd_id]['vnfm_dir'] +\
                                  '/hosts --ask-pass', timeout=None)
            child.expect('SSH password:')
            child.sendline(cfg.CONF.vnf.ssh_pwd)
            result =  child.readlines()
        self.agent_mapping[self.ns_dict[nsd_id]['vnfmanager_uuid']] =\
                      VNFManagerAgentApi(topics.get_topic_for_mgr(
                                     self.ns_dict[nsd_id]['vnfmanager_uuid']),
                                         cfg.CONF.host, self)
        nc = self.neutronclient
        body = {'port': {'binding:host_id': cfg.CONF.vnf.compute_hostname}}
        v_port_updated = nc.update_port(p_id,body)
        self.is_manager_invoked = True
        self._resolve_dependency(context, nsd_id)


    def _create_ovs_script(self, mgmt_id, nsd_id):
        nc = self.neutronclient
        v_port = nc.create_port({'port':{'network_id': mgmt_id}})
        p_id = v_port['port']['id']
        mac_address = v_port['port']['mac_address']
        lines_dict = []
        lines_dict.append('#!/bin/sh\n')
        lines_dict.append('sudo ovs-vsctl add-port br-int vtap-%s \
                -- set interface vtap-%s type=internal \
                -- set interface vtap-%s external-ids:iface-id=%s \
                -- set interface vtap-%s external-ids:iface-status=active \
                -- set interface vtap-%s external-ids:attached-mac=%s\n'
                %(str(p_id)[:8],str(p_id)[:8],str(p_id)[:8],str(p_id),
                  str(p_id)[:8],str(p_id)[:8],str(mac_address)))

        lines_dict.append('sudo ifconfig vtap-%s %s up\n'
                %(str(p_id)[:8],str(v_port['port']['fixed_ips'][0]\
                  ['ip_address'])))

        with open(self.ns_dict[nsd_id]['vnfm_dir']+'/ovs.sh', 'w') as f:
            f.writelines(lines_dict)
        return self.ns_dict[nsd_id]['vnfm_dir']+'/ovs.sh',str(p_id)


    def build_acknowledge_list(self, vnfd_name, vdu_name, instance, status, nsd_id):
        vdu = vnfd_name+':'+vdu_name
        if status == 'ERROR':
            self.update_nsd_status(nsd_id, 'ERROR')
            raise exceptions.ConfigurationError
        else:
            #check whether the key exists or not
            if self.ns_dict[nsd_id]['acknowledge_list'].get(vdu, None):
                self.ns_dict[nsd_id]['acknowledge_list'][vdu].append(instance)
            else:
                self.ns_dict[nsd_id]['acknowledge_list'][vdu] = [instance]

            # Check whether all the instances of a specific VDU 
            # are acknowledged
            vdu_instances = len(self.ns_dict[nsd_id]['vnfds'] \
                                [vnfd_name]['vdus'][vdu_name]['instances'])
            current_instances = len(self.ns_dict[nsd_id] \
                                    ['acknowledge_list'][vdu])
            if vdu_instances == current_instances:
                self.ns_dict[nsd_id]['created'].append(vdu)

 
class VNFManagerAgentApi(v_rpc.RpcProxy):
    """Plugin side of plugin to agent RPC API."""

    API_VERSION = '1.0'

    def __init__(self, topic, host, plugin):
        super(VNFManagerAgentApi, self).__init__(topic, self.API_VERSION)
        self.host = host
        self.plugin = plugin


    def configure_vdus(self, context, conf):
        return self.cast(
            context,
            self.make_msg('configure_vdus', conf=conf),
        )


class VNFManagerCallbacks(v_rpc.RpcCallback):
    RPC_API_VERSION = '1.0'

    def __init__(self, plugin):
        super(VNFManagerCallbacks, self).__init__()
        self.plugin = plugin

    def send_ack(self, context, vnfd, vdu, instance, status, nsd_id):
        if status == 'COMPLETE':
            self.plugin.build_acknowledge_list(vnfd, vdu, instance,
                                               status, nsd_id)
            LOG.debug(_('ACK received from VNFManager: '
                        'Configuration complete for VNF %s'), instance)
        else:
            self.plugin.build_acknowledge_list(vnfd, vdu, instance,
                                               status, nsd_id)
            LOG.debug(_('ACK received from VNFManager: '
                        'Confguration failed for VNF %s'), instance)
