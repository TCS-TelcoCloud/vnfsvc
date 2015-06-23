# Copyright 2014 Tata Consultancy Services, Inc.
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

"""
Network Descriptor parser

"""

import uuid
import yaml

from vnfsvc.client import client

unavailable_keys = [
    'flavour-id', 'description', 'template', 
    'vm_details', 'preconfigure', 'postconfigure', 
    'vnf-flavour', 'member-vnf-id', 'dependency', 'dependency_solved']
class NetworkParser(object):

    def __init__(self, yaml_stream=None):
        self.neutronclient = client.NeutronClient()
        if yaml_stream is not None:

            self.nsd = yaml_stream
            self.new_nsd = dict()
            self.remove_keys(['name', 'vendor', 'description', 'version'])
            self.new_nsd['preconfigure'] = dict()
            self.new_nsd['postconfigure'] = dict()

    def remove_keys(self, key_list):
        for key in key_list:
            if key in self.nsd.keys():
                del self.nsd[key]

    def parse(self, flavour, networks, router, subnets):
        self.flavour = flavour
        self.subnets =subnets
        self.networks = networks
        self.router = router
        self.nsd.update(self.nsd['flavours'][self.flavour])
        del self.nsd['flavours']
        self.member_vnfs(self.nsd['member-vnfs'])
        self.member_vlds(self.nsd['member-vlds'])
        self.forwarding_graphs(self.nsd['forwarding-graphs'])
        self.remove_keys(['member-vnfs', 'member-vlds', 'forwarding-graphs'])
        for key in self.nsd:
            if key not in unavailable_keys:
                method_key = key.replace('-','_')
                getattr(self, method_key)(self.nsd[key])
        mgmt_subnet = self.neutronclient.show_subnet(self.new_nsd['networks']['mgmt-if']['subnet_id'])
        self.new_nsd['mgmt-cidr'] = mgmt_subnet['subnet']['cidr']
        return self.new_nsd

    def monitoring(self, data):
        self.new_nsd['postconfigure']['monitoring'] = data

    def endpoints(self, data):
        self.new_nsd['postconfigure']['endpoints'] = data

    def assurance_params(self, data):
        self.new_nsd['postconfigure']['assurance_params'] = data

    def cfg_engine(self, data):
        self.new_nsd['preconfigure']['cfg_engine'] = data

    def member_vnfs(self, data):
        self.new_nsd['vnfds'] = dict()
        self.new_nsd['vdus'] = dict()
        for vdu in data:
            if vdu['name'] not in self.new_nsd['vnfds'].keys(): 
                self.new_nsd['vnfds'][vdu['name']] = list()
            self.new_nsd['vnfds'][vdu['name']].append(vdu['member-vdu-id'])
            vdu_name = vdu['name']+':'+vdu['member-vdu-id']
            self.new_nsd['vdus'][vdu_name] =dict()
            self.new_nsd['vdus'][vdu_name]['id'] = str(uuid.uuid4())
            if 'dependency' in vdu.keys():
                if isinstance(vdu['dependency'], list):
                    self.new_nsd['vdus'][vdu_name]['dependency'] = vdu['dependency']
                else:
                    self.new_nsd['vdus'][vdu_name]['dependency'] = [vdu['dependency']]

    def member_vlds(self, data):
        self.new_nsd['networks'] = dict()
        for network in data:
            temp_dict = dict()
            temp_dict['id'] = self.networks[network]
            temp_dict['property'] = data[network]['property']
            #@TODO:Assuming one subnet for one network --- need to modify
            temp_dict['subnet_id'] = self.subnets[network]
            if 'Router' in data[network].keys():
                temp_dict['Router'] = self.router
                self.new_nsd['preconfigure']['router'] = {
                    'name': self.router, 
                    'network': self.networks[network],
                    'subnet_id': self.subnets[network],
                    'if_name':  data[network]['Router']}
            self.new_nsd['networks'][network] = temp_dict
            self.add_networks(data[network]['member-vnfs'],self.networks[network], self.subnets[network])

    def add_networks(self, data, network_id, subnet_id):
        if data is None:
            return
        for key in data:
            interfaces = data[key]['connection-point']
            if type(interfaces) == type([]):
                for interface in interfaces:
                    if 'networks' not in self.new_nsd['vdus'][key].keys():
                        self.new_nsd['vdus'][key]['networks'] = dict()    
                    self.new_nsd['vdus'][key]['networks'][interface] = {'net-id': network_id, 'subnet-id':subnet_id}
            else:
                if 'networks' not in self.new_nsd['vdus'][key].keys():
                    self.new_nsd['vdus'][key]['networks'] = dict()
                self.new_nsd['vdus'][key]['networks'][interfaces] = {'net-id': network_id, 'subnet-id':subnet_id}  

    def forwarding_graphs(self, data):
        self.new_nsd['postconfigure']['forwarding_graphs'] = data

    def get_forwarding_graph(self, nsd_template):
        return nsd_template['postconfigure']['forwarding_graphs']['WebAccess']['network-forwarding-path']
