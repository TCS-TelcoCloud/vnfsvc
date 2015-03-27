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
VNF Descriptor parser

"""

import uuid
import yaml

from vnfsvc.client import client

unavailable_keys = [
	'flavour-id', 'description', 
	'vm_details', 'preconfigure', 'postconfigure', 
	'vnf-flavour', 'member-vnf-id', 'dependency', 'dependency_solved']

class VNFParser(object):
    def __init__(self, vnfd=None, flavour=None, vdu_list=None, vnfd_name=None, nsd=None):
        self.neutronclient = client.NeutronClient()
        if vnfd == None:
            self.vnfd = dict()
        else:
            self.vnfd_name = vnfd_name
            self.vdu_list = vdu_list
            self.nsd = nsd
            vnfd['template']['vnfd'] = self.remove_keys(vnfd['template']['vnfd'], ['id','vendor', 'description', 'version'])
            vnfd.update(vnfd['template']['vnfd']['flavours'][flavour])
            del vnfd['template']['vnfd']['flavours']
            self.vnfd = vnfd
            self.new_vnfd = dict()
            self.new_vnfd['preconfigure'] = dict()
            self.new_vnfd['postconfigure'] = dict()
            self.new_vnfd['vdu_keys'] = list()
            self.new_vnfd['vdus'] = dict()
            for vdu in self.vnfd['vdus']:
                if vdu not in vdu_list:
                    continue
                self.new_vnfd['vdu_keys'].append(vdu)
                self.new_vnfd['vdus'][vdu] = dict()
                self.new_vnfd['vdus'][vdu]['vm_details'] = dict()
                self.new_vnfd['vdus'][vdu]['preconfigure'] = dict()
                self.new_vnfd['vdus'][vdu]['postconfigure'] = dict()
    
    def remove_keys(self, dict, key_list):
        for key in key_list:
            del dict[key]
        return dict
        
    def parse(self):
        for key in self.vnfd:
            if key not in unavailable_keys :
                if key == 'vdus':
                    continue
                else:
	                method_key = key.replace('-','_')
	                getattr(self, method_key)(self.vnfd[key])
        self.vdus(self.vnfd['vdus'])
        del self.vnfd['vdus']
        del self.vnfd['template']
        return self.new_vnfd

    def template(self, data):
        for key in data['vnfd']:
            method_key = key.replace('-','_')
            getattr(self, method_key)(data['vnfd'][key])

    def num_instances(self, data, vdu):
        self.new_vnfd['vdus'][vdu]['vm_details']['num-instances'] = data
	   
    def lifecycle_events(self, data, vdu):
	    self.new_vnfd['vdus'][vdu]['postconfigure']['lifecycle_events'] = data

    def vm_spec(self, data, vdu):
	    self.new_vnfd['vdus'][vdu]['vm_details']['image_details'] = data

    def storage(self, data, vdu):
	    self.new_vnfd['vdus'][vdu]['vm_details']['disk'] = data

    def cpu(self, data, vdu):
	    self.new_vnfd['vdus'][vdu]['vm_details']['vcpus'] = data['num-vcpu']

    def memory(self, data, vdu):
	    self.new_vnfd['vdus'][vdu]['vm_details']['ram'] = data['total-memory-mb']

    def other_constraints(self, data, vdu):
	    if data:
	        self.new_vnfd['vdus'][vdu]['preconfigure']['other_constraints'] = data

    def network_interfaces(self, data, vdu):
        self.new_vnfd['vdus'][vdu]['vm_details']['network_interfaces'] = data
        ni = self.new_vnfd['vdus'][vdu]['vm_details']['network_interfaces']
        for key in data:
            ref = ni[key]['connection-point-ref'].split('/')[1]
            ni[key].update(self.nsd['vdus'][self.vnfd_name+':'+vdu]['networks'][ref])
            if 'fixed-ip' in self.new_vnfd['connection_point'][ref].keys():
                fixed_ip = self.new_vnfd['connection_point'][ref]['fixed-ip']
                subnet_id = ni[key]['subnet-id']
                net_id = ni[key]['net-id']
                if fixed_ip == 'gateway':
                    subnet = self.neutronclient.show_subnet(subnet_id)
                    gateway_ip = subnet['subnet']['gateway_ip']
                    port_dict = {'port':{}}
                    port_dict['port']['network_id'] = net_id
                    port_dict['port']['fixed_ips'] = [{
                        'subnet_id': subnet_id,
                        'ip_address': gateway_ip
                    }]
                    port_dict['port']['admin_state_up'] = True
                    port = self.neutronclient.create_port(port_dict)
                    ni[key]['port_id'] = port['port']['id']
            if 'properties' in ni[key].keys():
                self.new_vnfd['vdus'][vdu]['mgmt-driver'] = ni[key]['properties']['driver']

    def assurance_params(self, data):
 	    self.new_vnfd['postconfigure']['assurance_params'] = data

    def implementation_artifact(self, data, vdu):
        artifact_dict = dict()
        self.new_vnfd['vdus'][vdu]['vm_details']['userdata'] = data['deployment_artifact']
        artifact_dict['cfg_engine'] = data.get('cfg_engine', None)
        artifact_dict['deployment_artifact'] = data['deployment_artifact']
        if 'implementation_artifact' in self.new_vnfd['vdus'][vdu]['preconfigure'].keys():
            self.new_vnfd['vdus'][vdu]['preconfigure']['implementation_artifact'].append(artifact_dict)
        else:
            self.new_vnfd['vdus'][vdu]['preconfigure']['implementation_artifact'] = [artifact_dict]    

    def connection_point(self, data):
        self.new_vnfd['connection_point'] = data

    def monitoring_parameter(self, data):
	    self.new_vnfd['postconfigure']['monitoring_parameter'] = data

    def vdus(self, data):
        for vdu in data.keys():
            if vdu not in self.vdu_list:
                continue
            #self.new_vnfd['vdus'][vdu]['id'] = str(uuid.uuid4())
            for key in data[vdu]:
                if key != 'vdu-id':
                    method_key = key.replace('-','_')
                    getattr(self, method_key)(data[vdu][key], vdu)

    def get_flavor_dict(self, vdu):
        flavor_dict = dict()
        flavor_dict['ram'] = vdu['vm_details']['ram']
        flavor_dict['vcpus'] = vdu['vm_details']['vcpus']
        flavor_dict['disk'] = vdu['vm_details']['disk']
        flavor_dict['name'] = str(uuid.uuid4()).split('-')[0]
        return flavor_dict

    def get_boot_details(self, vdu):
        """ Returns the dictionary that contains all necessary information to launch a VM """
        boot_dict = dict()
        boot_dict['image'] = vdu['vm_details']['image_details']
        boot_dict['num_instances'] = vdu['vm_details']['num-instances']        
        if 'userdata' in vdu['vm_details'].keys():
            boot_dict['userdata'] = vdu['vm_details']['userdata']
        return boot_dict



