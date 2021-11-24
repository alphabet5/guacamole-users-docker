import os
import sqlalchemy
from time import sleep
import pymysql
from ldap3 import Server, Connection, ALL, SUBTREE
import yaml
import hashlib
from datetime import datetime
import uuid
from rich.console import Console
from rich.traceback import install
from rich import print
import json
from copy import deepcopy
from collections import defaultdict
import re
import threading
from sqlalchemy.sql import text
import dns.resolver


def sql_insert(engine, conn, table, **kwargs):
    import sqlalchemy
    from sqlalchemy.dialects.mysql import insert
    metadata = sqlalchemy.MetaData()
    table_obj = sqlalchemy.Table(table, metadata, autoload=True, autoload_with=engine)
    insert_statement = insert(table_obj).values(**kwargs)
    on_duplicate = insert_statement.on_duplicate_key_update(**kwargs)
    return conn.execute(on_duplicate)


def wait_for_sql(engine, retries=60):
    for i in range(retries):
        try:
            with engine.begin() as sql_conn:
                sql_conn.execute('SELECT 1')
                return True
        except pymysql.err.OperationalError:
            print("Cannot connect to mysql. Waiting...")
            sleep(1)
    print("Error connecting to MySQL. Exiting.")
    return False


def wait_for_ldap(ldap_info, retries=60):
    print_traceback = True
    for i in range(retries):
        try:
            server = Server(ldap_info['ldap-hostname'],
                            get_info=ALL)
            ldap_conn = Connection(server=server,
                                   user=ldap_info['ldap-search-bind-dn'],
                                   password=ldap_info['ldap-search-bind-password'],
                                   auto_bind=True)
            return True
        except:
            if print_traceback:
                console.print_exception(show_locals=True)
                print_traceback = False
            print("Cannot connect to ldap server. Waiting...")
            sleep(1)
    print("Error connecting to ldap. Exiting.")
    return False


def get_ldap_mysql():
    # Connect to SQL
    engine = sqlalchemy.create_engine('mysql+pymysql://' +
                                      os.environ['MYSQL_USER'] + ':' +
                                      os.environ['MYSQL_PASSWORD'] + '@' +
                                      os.environ['MYSQL_HOSTNAME'] + ':3306/' +
                                      os.environ['MYSQL_DB'])
    if not wait_for_sql(engine):
        return False
    ldap_info = yaml.load(open('/configs/guacamole.properties', 'r'), yaml.FullLoader)
    if not wait_for_ldap(ldap_info):
        return False
    # Fetch LDAP information
    server = Server(ldap_info['ldap-hostname'],
                    get_info=ALL)
    ldap_conn = Connection(server=server,
                           user=ldap_info['ldap-search-bind-dn'],
                           password=ldap_info['ldap-search-bind-password'],
                           auto_bind=True)
    return engine, ldap_conn, ldap_info


def init_sql():
    engine, ldap_conn, ldap_info = get_ldap_mysql()

    with engine.connect() as sql_conn:
        with open('/templates/initdb.sql.script', 'r') as f:
            query = text(f.read())
            sql_conn.execute(query)


def update_connections():

    engine, ldap_conn, ldap_info = get_ldap_mysql()
    ldap_conn.search(search_base=os.environ['LDAP_COMPUTER_BASE_DN'],
                     search_scope=SUBTREE,
                     search_filter=os.environ['LDAP_COMPUTER_FILTER'],
                     attributes=['cn', 'dNSHostName'])
    ldap_computers = json.loads(ldap_conn.response_to_json())

    # Create connections
    auto_conn_parameters = yaml.load(open('/configs/auto-connections.yaml', 'r'), yaml.FullLoader)
    # computer_cn = dict()
    with engine.begin() as sql_conn:
        # Create guacadmin user and update password.
        metadata = sqlalchemy.MetaData()
        guacamole_entity = sqlalchemy.Table('guacamole_entity', metadata, autoload=True, autoload_with=engine)
        # guacamole_user = sqlalchemy.Table('guacamole_user', metadata, autoload=True, autoload_with=engine)
        sql_insert(engine, sql_conn, 'guacamole_entity',
                   name='guacadmin',
                   type='USER')
        entity_id = sqlalchemy.select([guacamole_entity]).where(
            guacamole_entity.columns.name == 'guacadmin')
        result = sql_conn.execute(entity_id)
        entity_id_value = result.fetchone()[0]
        password_salt = hashlib.sha256(str(uuid.uuid1().bytes).encode('utf-8'))
        password_hash = hashlib.sha256(
            (os.environ['GUACADMIN_PASSWORD'] + password_salt.hexdigest().upper()).encode('utf-8'))
        sql_insert(engine, sql_conn, 'guacamole_user',
                   entity_id=entity_id_value,
                   password_hash=password_hash.digest(),
                   password_salt=password_salt.digest(),
                   password_date=datetime.now())
        connections = list()
        connection_ids = list()
        # name_cn_id = defaultdict(lambda: {})
        for computer in ldap_computers['entries']:
            auto_conn_dns = os.environ['CFG_AUTO_CONNECTION_DNS']
            if auto_conn_dns in ['true', 't', 'y', 'yes']:
                hostname = computer['attributes']['dNSHostName']
                conn_name = hostname
            else:
                dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
                dns.resolver.default_resolver.nameservers = [os.environ['CFG_AUTO_CONNECTION_DNS_RESOLVER']]
                hostname = dns.resolver.resolve(computer['attributes']['dNSHostName'], 'a').response.answer[0][0].address
                conn_name = computer['attributes']['dNSHostName'] + " - " + hostname
            connection = auto_conn_parameters
            connection['connection']['connection_name'] = conn_name
            connection['parameters']['hostname'] = hostname
            connections.append(deepcopy(connection))
            # name_cn_id[conn_name]['cn'] = computer['attributes']['cn']
        if os.path.isfile('/configs/manual-connections.yaml'):
            manual_connections = yaml.load(open('/configs/manual-connections.yaml', 'r'), yaml.FullLoader)
            defaults = manual_connections['manual_connections']['defaults']
            for connection in manual_connections['manual_connections']['connections']:
                new_connection = dict()
                if connection['defaults']:
                    new_connection['connection'] = defaults['connection'] | connection['connection']
                    new_connection['parameters'] = defaults['parameters'] | connection['parameters']
                else:
                    new_connection['connection'] = connection['connection']
                    new_connection['parameters'] = connection['parameters']
                connections.append(deepcopy(new_connection))

        connection_name_ids = dict()
        for connection in connections:
            sql_insert(engine, sql_conn, 'guacamole_connection',
                       **connection['connection'])
            conn_name = connection['connection']['connection_name']
            connection_id = \
                sql_conn.execute('SELECT connection_id from guacamole_connection WHERE connection_name = "' +
                                 conn_name + '";').fetchone()['connection_id']
            # name_cn_id[connection['connection']['connection_name']]['id'] = connection_id
            connection_ids.append(connection_id)
            for parameter_name, parameter_value in connection['parameters'].items():
                sql_insert(engine, sql_conn, 'guacamole_connection_parameter',
                           connection_id=connection_id,
                           parameter_name=parameter_name,
                           parameter_value=parameter_value)
            # Remove undefined parameters.
            sql_conn.execute("DELETE FROM guacamole_connection_parameter WHERE connection_id = " + str(connection_id) + " AND parameter_name NOT IN ('" + "','".join(connection['parameters'].keys()) + "');")

        # Clean up undefined connections.
        connections = sql_conn.execute('SELECT * from guacamole_connection;').fetchall()
        for connection in connections:
            if connection['connection_id'] not in connection_ids:
                sql_conn.execute(
                    'DELETE from guacamole_connection WHERE connection_id = ' + str(
                        connection['connection_id']) + ';')
                sql_conn.execute(
                    'DELETE from guacamole_connection_parameter WHERE connection_id = ' + str(
                        connection['connection_id']) + ';')
    threading.Timer(60, update_connections)

def update_users():
    engine, ldap_conn, ldap_info = get_ldap_mysql()

    # Create list of LDAP Groups that contain all sub-groups.
    admin_groups = os.environ['GUAC_ADMIN_GROUPS'].split(',')
    groups = dict()
    ldap_conn.search(search_base=ldap_info['ldap-group-base-dn'],
                     search_scope=SUBTREE,
                     search_filter='(objectCategory=Group)',
                     attributes=['cn', 'memberOf'])
    ldap_entries = json.loads(ldap_conn.response_to_json())

    # List parent groups. admin + manual + regex
    # Add conn id's for parent groups. admin + manual + regex
    parent_groups = defaultdict(lambda: [])
    conn_ids = dict()
    with engine.begin() as sql_conn:
        for conn in sql_conn.execute('SELECT * FROM guacamole_connection;').fetchall():
            conn_ids[conn['connection_name']] = conn['connection_id']
    # Add the groups from the manually defined connections.
    if os.path.isfile('/configs/manual-connections.yaml'):
        manual_connections = yaml.load(open('/configs/manual-connections.yaml', 'r'), yaml.FullLoader)
        for group in manual_connections['manual_permissions']:
            for conn_name in manual_connections['manual_permissions'][group]:
                # This is appending the connection id for each named connection in the manual_permissions section.
                parent_groups[group].append(conn_ids[conn_name])
    # Add the groups from the regular expression defining the group name from the connection name.
    nested_groups = defaultdict(lambda: [])
    nested_groups_cn = dict()
    for conn_name, conn_id in conn_ids.items():
        regex_result = re.match(os.environ['LDAP_GROUP_NAME_FROM_CONN_NAME_REGEX'], conn_name).group(1)
        if regex_result is not None:
            group_name = os.environ['LDAP_GROUP_NAME_MOD'].replace('{regex}', regex_result)
            for group in ldap_entries['entries']:
                if group['attributes']['cn'] == group_name:
                    parent_groups[group_name].append(conn_id)
                    nested_groups[group_name].append(group['dn'])
                    nested_groups_cn[group['dn']] = group['attributes']['cn']
                    break
    for i in range(4):
        for group_name, dn_list in nested_groups.items():
            for group in ldap_entries['entries']:
                for member_of in group['attributes']['memberOf']:
                    if member_of in dn_list:
                        nested_groups[group_name].append(group['dn'])
    for group, dn_list in nested_groups.items():
        for dn in dn_list:
            parent_groups[nested_groups_cn[dn]] += parent_groups[group]

    admin_groups = os.environ['GUAC_ADMIN_GROUPS'].split(',')
    for admin_group in admin_groups:
        for conn_id in conn_ids.values():
            parent_groups[admin_group].append(conn_id)

    group_permissions = dict()
    for group_name, ids in parent_groups.items():
        group_permissions[group_name] = list(set(ids))

    with engine.begin() as sql_conn:
        for group, conn_ids in group_permissions.items():
            sql_insert(engine, sql_conn, 'guacamole_entity',
                       **{'name': group, 'type': 'USER_GROUP'})

            entity_id = sql_conn.execute('SELECT entity_id from guacamole_entity WHERE name = "' +
                                         group + '";').fetchone()['entity_id']
            sql_insert(engine, sql_conn, 'guacamole_user_group',
                       **{'entity_id': entity_id,
                          'disabled': 0})
            if len(conn_ids) > 1:
                sql_conn.execute("DELETE FROM guacamole_connection_permission WHERE entity_id = " + str(
                    entity_id) + " AND connection_id NOT IN (" + ",".join(
                    [str(i) for i in conn_ids]) + ");")
            elif len(conn_ids) == 1:
                sql_conn.execute("DELETE FROM guacamole_connection_permission WHERE entity_id = " + str(
                    entity_id) + " AND connection_id = " + str(conn_ids[0]) + ";")
            if group in os.environ['GUAC_ADMIN_GROUPS'].split(','):
                for permission in ['CREATE_CONNECTION', 'CREATE_CONNECTION_GROUP',
                                   'CREATE_SHARING_PROFILE', 'CREATE_USER',
                                   'CREATE_USER_GROUP', 'ADMINISTER']:
                    sql_insert(engine, sql_conn, 'guacamole_system_permission',
                               **{'entity_id': entity_id,
                                  'permission': permission})
            for conn_id in conn_ids:
                if group not in os.environ['GUAC_ADMIN_GROUPS'].split(','):
                    permissions = ['READ']
                else:
                    permissions = ['READ', 'UPDATE', 'DELETE', 'ADMINISTER']
                for permission in permissions:
                    sql_insert(engine, sql_conn, 'guacamole_connection_permission',
                               **{'entity_id': entity_id,
                                  'connection_id': conn_id,
                                  'permission': permission})
    threading.Timer(120, update_users)


if __name__ == '__main__':
    # Install rich traceback for better diagnostics.
    console = Console()
    install(show_locals=True)
    #init_sql()
    from time import sleep
    while True:
        update_connections()
        update_users()
        sleep(30)




