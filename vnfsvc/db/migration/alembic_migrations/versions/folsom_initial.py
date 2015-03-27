# Copyright 2014 Tata Consultancy Services Ltd.
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
#

"""folsom initial database

Revision ID: folsom
Revises: None
Create Date: 2012-12-03 09:14:50.579765

"""


# revision identifiers, used by Alembic.
revision = 'folsom'
down_revision = None

from alembic import op
import sqlalchemy as sa

from vnfsvc.db import migration
# NOTE: This is a special migration that creates a Folsom compatible database.


def upgrade(active_plugins=None, options=None):
    # general model
    upgrade_base()


def upgrade_base():
    op.create_table(
        'networkservices',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('vnfm_id',sa.String(length=36), nullable=False),
        sa.Column('vdus',sa.String(4000), nullable=True),
        sa.Column('networks',sa.String(length=4000), nullable=True),
        sa.Column('subnets',sa.String(4000),nullable=False),
        sa.Column('router',sa.String(4000),nullable=False),
        sa.Column('service_type',sa.String(36),nullable=False),
        sa.Column('status',sa.String(36),nullable=False),
        #sa.Column('puppet_id',sa.String(36),nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'vdus',
        sa.Column('id',sa.String(36),nullable=False),
        sa.Column('instances',sa.String(4000),nullable=False),
        sa.Column('flavor',sa.String(36),nullable=False),
        sa.Column('image',sa.String(36),nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

def downgrade(active_plugins=None, options=None):
    downgrade_base()


def downgrade_base():
    drop_tables(
        'networkservices',
        'vdus'
    )

def drop_tables(*tables):
    for table in tables:
        op.drop_table(table)
