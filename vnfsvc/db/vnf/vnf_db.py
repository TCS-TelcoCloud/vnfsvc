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

import uuid
import ast

import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import exc as orm_exc

from vnfsvc.api.v2 import attributes
from vnfsvc.common import exceptions
from vnfsvc.db import common_db_mixin as base_db
from vnfsvc.db import model_base
from vnfsvc import manager
from vnfsvc.openstack.common import jsonutils
from vnfsvc.openstack.common import log as logging
from vnfsvc import constants
from vnfsvc.openstack.common import uuidutils
from vnfsvc.openstack.common.gettextutils import _
from vnfsvc.api.v2 import vnf

LOG = logging.getLogger(__name__)
_ACTIVE_UPDATE = (constants.ACTIVE, constants.PENDING_UPDATE)

service_db_dict={}
class HasTenant(object):
    """Tenant mixin, add to subclasses that have a tenant."""

    tenant_id = sa.Column(sa.String(255))


class HasId(object):
    """id mixin, add to subclasses that have an id."""

    id = sa.Column(sa.String(36),
                   primary_key=True,
                   default=uuidutils.generate_uuid)


class NetworkService(model_base.BASEV2):
    """Represents binding of Network service details
    """
    id = sa.Column(sa.String(36), primary_key=True, nullable=False)
    vnfm_id = sa.Column(sa.String(4000),nullable=False)
    vdus = sa.Column(sa.String(4000), nullable=False)
    networks = sa.Column(sa.String(4000), nullable=False)
    subnets = sa.Column(sa.String(4000), nullable=False)
    router = sa.Column(sa.String(4000), nullable=False)
    service_type = sa.Column(sa.String(36), nullable=False)
    #puppet_id = sa.Column(sa.String(36), nullable=False)
    status = sa.Column(sa.String(36), nullable=False)


class Vdu(model_base.BASEV2):
    """Represents VDU details
    """
    id = sa.Column(sa.String(36), primary_key=True,nullable=False)
    instances = sa.Column(sa.String(4000),nullable=False)
    flavor = sa.Column(sa.String(36),nullable=False)
    image = sa.Column(sa.String(36),nullable=False)


###########################################################################

class NetworkServicePluginDb(base_db.CommonDbMixin):

    @property
    def _core_plugin(self):
        return manager.VnfsvcManager.get_plugin()


    def subnet_id_to_network_id(self, context, subnet_id):
        subnet = self._core_plugin.get_subnet(context, subnet_id)
        return subnet['network_id']


    def __init__(self):
        super(NetworkServicePluginDb, self).__init__()


    def _make_service_dict(self, service_db, fields=None):
        LOG.debug(_('service_db %s'), service_db)
        res = {}
        key_list = ('id', 'vnfm_id', 'vdus', 'networks', 'subnets','router','service_type','status')
        res.update((key, service_db[key]) for key in key_list)
        return self._fields(res, fields)


    def populate_vdu_details(self, context, nsd):
        vdus = dict()
        with context.session.begin(subtransactions=True):
            for vdu in nsd['vdus']:
                vdus[vdu] = nsd['vdus'][vdu]['id']
                vdu_db = Vdu(id=vdus[vdu], instances='', flavor='', image='')
                context.session.add(vdu_db)
        return vdus


    def update_vdu_details(self, context, flavor_id, image_id, vdu_id):
        with context.session.begin(subtransactions=True):
            vdu = self._model_query(context, Vdu).filter(Vdu.id==vdu_id).first()
            if vdu:
                vdu.update({
                    'flavor': flavor_id,
                    'image': image_id
                    })
            else:
                raise exceptions.NoSuchVDUException()
            context.session.add(vdu)

    def update_vdu_instance_details(self, context,instance_id, vdu_id):
        with context.session.begin(subtransactions=True):
            vdu = self._model_query(context, Vdu).filter(Vdu.id==vdu_id).first()
            if vdu:
                if vdu.instances!='':
                    instances = vdu.instances+','+instance_id
                else:
                    instances = instance_id
                vdu.update({
                    'instances': instances
                    })
            else:
                raise exceptions.NoSuchVDUException()
            context.session.add(vdu)

    def update_nsd_status(self, context, nsd_id, status):
        with context.session.begin(subtransactions=True):
            nsd = self._model_query(context, NetworkService).filter(NetworkService.id==nsd_id).first()
            if nsd:
                nsd.update({
                    'status': status
                    })
            else:
                raise exceptions.NoSuchNSDException()
            context.session.add(nsd)

    def create_service_model(self, context, **db_dict):
        nsd = db_dict['nsd']
        with context.session.begin(subtransactions=True):
            id = db_dict['id'] 
            vnfm_id = db_dict['vnfm_id']
            networks = str(db_dict['networks'])
            subnets = str(db_dict['subnets'])
            router = nsd['preconfigure']['router']
            if_name = router['if_name']
            router['id'] = nsd['router'][if_name]['id']
            service_type = db_dict['service']['name']
            vdus = str(self.populate_vdu_details(context, nsd))
            #puppet = nsd.get('puppet-master', None)
            #if puppet:
            #    puppet_id = puppet.get('instance_id', '')
            #else:
            #    puppet_id = ''
            status = db_dict['status']
            service_db = NetworkService(id=id, vnfm_id=vnfm_id, networks=networks, subnets=subnets, 
                    vdus=vdus, router=str(router), service_type=service_type, status=status)
            context.session.add(service_db)
            return self._make_service_dict(service_db)

    def delete_service_model(self, context, nsd_id):
        instances_list=[]
        try:
            with context.session.begin(subtransactions=True):
                service_db = self._model_query(context, NetworkService).filter(NetworkService.id == nsd_id).all()
                service_db_dict['service_db']=service_db
                service_dict = service_db[0].__dict__
                vduid_list = ast.literal_eval(service_dict['vdus']).values()
                for vdu_id in range(len(vduid_list)):
                   instances = self._model_query(context, Vdu).filter(Vdu.id == vduid_list[vdu_id]).all()
                   instances_list.append(instances)
                service_db_dict['instances']=instances_list
        except Exception as e:
            return None
        return service_db_dict

    def delete_db_dict(self, context, nsd_id):
        service_db = self._model_query(context, NetworkService).filter(NetworkService.id == nsd_id).all()
        service_dict = service_db[0].__dict__
        vduid_list = ast.literal_eval(service_dict['vdus']).values()
        for vdu_id in range(len(vduid_list)):
           instances = self._model_query(context, Vdu).filter(Vdu.id == vduid_list[vdu_id]).all()
           self._model_query(context, Vdu).filter(Vdu.id == vduid_list[vdu_id]).delete()
        self._model_query(context, NetworkService).filter(NetworkService.id == nsd_id).delete()

    def get_service_model(self, context, nsd_id, fields=None):
        try:
            service_db = self._model_query(context, NetworkService).filter(NetworkService.id == nsd_id).one()
            return self._make_service_dict(service_db, fields)
        except Exception:
            return {}

    def get_all_services(self, context, filters=None, fields=None):
        return self._get_collection(context, NetworkService, self._make_service_dict,
                                    filters=filters, fields=fields)

