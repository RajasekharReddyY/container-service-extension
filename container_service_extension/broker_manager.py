# container-service-extension
# Copyright (c) 2019 VMware, Inc. All Rights Reserved.
# SPDX-License-Identifier: BSD-2-Clause

from collections import namedtuple
from enum import Enum
from enum import unique
from http import HTTPStatus

from pyvcloud.vcd.org import Org

from container_service_extension.exceptions import ClusterNotFoundError
from container_service_extension.exceptions import CseServerError
from container_service_extension.exceptions import PksServerError
from container_service_extension.logger import SERVER_LOGGER as LOGGER
from container_service_extension.ovdc_cache import CONTAINER_PROVIDER_KEY
from container_service_extension.ovdc_cache import CtrProvType
from container_service_extension.ovdc_cache import OvdcCache
from container_service_extension.pksbroker import PKSBroker
from container_service_extension.utils import ACCEPTED
from container_service_extension.utils import connect_vcd_user_via_token
from container_service_extension.utils import exception_handler
from container_service_extension.utils import get_pks_cache
from container_service_extension.utils import get_server_runtime_config
from container_service_extension.utils import get_vcd_sys_admin_client
from container_service_extension.utils import OK
from container_service_extension.vcdbroker import VcdBroker


# TODO(Constants)
#  1. Scan and classify all broker-related constants in server code into
#  either common, vcd_broker_specific, pks_broker_specific constants.
#  Design and refactor them into one or more relevant files.
#  2. Scan through both CSE client and server to identify HTTP request/response
#  body params and define all of them as constants into a file
#  from where both client and server can access them.
#  3. Refactor both client and server code accordingly
#  4. As part of refactoring, avoid accessing HTTP request body directly
#  from VcdBroker and PksBroker. We should try to limit processing request to
#  processor.py and broker_manager.py.
@unique
class Operation(Enum):
    CREATE_CLUSTER = 'create cluster'
    DELETE_CLUSTER = 'delete cluster'
    GET_CLUSTER = 'get cluster info'
    LIST_CLUSTERS = 'list clusters'
    RESIZE_CLUSTER = 'resize cluster'
    LIST_OVDCS = 'list ovdcs'
    ENABLE_OVDC = 'enable ovdc'
    INFO_OVDC = 'info ovdc'
    GET_CLUSTER_CONFIG = 'get cluster config'


class BrokerManager(object):
    """Manage calls to vCD and PKS brokers.

    Handles:
    Pre-processing of requests to brokers
    Post-processing of results from brokers.
    """

    def __init__(self, request_headers, request_query_params, request_spec):
        self.req_headers = request_headers
        self.req_qparams = request_query_params
        self.req_spec = request_spec
        self.pks_cache = get_pks_cache()
        self.ovdc_cache = OvdcCache(get_vcd_sys_admin_client())
        self.is_ovdc_present_in_request = False
        config = get_server_runtime_config()
        self.vcd_client, self.session = connect_vcd_user_via_token(
            vcd_uri=config['vcd']['host'],
            headers=self.req_headers,
            verify_ssl_certs=config['vcd']['verify'])

    @exception_handler
    def invoke(self, op):
        """Invoke right broker(s) to perform the operation requested.

        Might result in further (pre/post)processing on the request/result(s).


        Depending on the operation requested, this method may do one or more
        of below mentioned points.
        1. Extract and construct the relevant params for the operation.
        2. Choose right broker to perform the operation/method requested.
        3. Scan through available brokers to aggregate (or) filter results.
        4. Construct and return the HTTP response

        :param Operation op: Operation to be performed by one of the brokers.

        :return result: HTTP response

        :rtype: dict
        """
        result = {}
        result['body'] = []
        result['status_code'] = OK

        self.is_ovdc_present_in_request = self.req_spec.get('vdc') or \
            self.req_qparams.get('vdc')
        if op == Operation.INFO_OVDC:
            ovdc_id = self.req_spec.get('ovdc_id')
            # TODO() Constructing response should be moved out of this layer
            result['body'] = self.ovdc_cache. \
                get_ovdc_container_provider_metadata(ovdc_id=ovdc_id)
            result['status_code'] = OK
        elif op == Operation.ENABLE_OVDC:
            pks_ctx, ovdc = self._get_ovdc_params()
            if self.req_spec[CONTAINER_PROVIDER_KEY] == CtrProvType.PKS.value:
                self._create_pks_compute_profile(pks_ctx)
            task = self.ovdc_cache. \
                set_ovdc_container_provider_metadata(
                    ovdc,
                    container_prov_data=pks_ctx,
                    container_provider=self.req_spec[CONTAINER_PROVIDER_KEY])
            # TODO() Constructing response should be moved out of this layer
            result['body'] = {'task_href': task.get('href')}
            result['status_code'] = ACCEPTED
        elif op == Operation.GET_CLUSTER:
            cluster_spec = \
                {'cluster_name': self.req_spec.get('cluster_name', None)}
            result['body'] = self._get_cluster_info(**cluster_spec)[0]
        elif op == Operation.LIST_CLUSTERS:
            result['body'] = self._list_clusters()
        elif op == Operation.DELETE_CLUSTER:
            cluster_spec = \
                {'cluster_name': self.req_spec.get('cluster_name', None)}
            result['body'] = self._delete_cluster(**cluster_spec)
            result['status_code'] = ACCEPTED
        elif op == Operation.RESIZE_CLUSTER:
            # TODO(resize_cluster) Once VcdBroker.create_nodes() is hooked to
            #  broker_manager, ensure broker.resize_cluster returns only
            #  response body. Construct the remainder of the response here.
            #  This cannot be done at the moment as @exception_handler cannot
            #  be removed on create_nodes() as of today (Mar 15, 2019).
            cluster_spec = \
                {'cluster_name': self.req_spec.get('cluster_name', None),
                 'node_count': self.req_spec.get('node_count', None)
                 }
            result['body'] = self._resize_cluster(**cluster_spec)
            result['status_code'] = ACCEPTED
        elif op == Operation.GET_CLUSTER_CONFIG:
            cluster_spec = \
                {'cluster_name': self.req_spec.get('cluster_name', None)}
            result['body'] = \
                self._get_cluster_config(**cluster_spec)
        elif op == Operation.CREATE_CLUSTER:
            # TODO(ClusterSpec) Create an inner class "ClusterSpec"
            #  in abstract_broker.py and have subclasses define and use it
            #  as instance variable.
            #  Method 'Create_cluster' in VcdBroker and PksBroker should take
            #  ClusterSpec either as a param (or)
            #  read from instance variable (if needed only).
            cluster_spec = {
                'cluster_name': self.req_spec.get('cluster_name', None),
                'vdc_name': self.req_spec.get('vdc', None),
                'node_count': self.req_spec.get('node_count', None),
                'storage_profile': self.req_spec.get('storage_profile', None),
                'network_name': self.req_spec.get('network', None),
                'template': self.req_spec.get('template', None),
                'pks_plan': self.req_spec.get('pks_plan', None),
                'pks_ext_host': self.req_spec.get('pks_ext_host', None)
            }
            result['body'] = self._create_cluster(**cluster_spec)
            result['status_code'] = ACCEPTED
        elif op == Operation.LIST_OVDCS:
            result['body'] = self._list_ovdcs()

        return result

    def _list_ovdcs(self):
        """Get list of ovdcs.

        If client is sysadmin,
            Gets all ovdcs of all organizations.
        Else
            Gets all ovdcs of the organization in context.
        """
        if self.vcd_client.is_sysadmin():
            org_resource_list = self.vcd_client.get_org_list()
        else:
            org_resource_list = list(self.vcd_client.get_org())

        ovdc_list = []
        for org_resource in org_resource_list:
            org = Org(self.vcd_client, resource=org_resource)
            vdc_list = org.list_vdcs()
            for vdc in vdc_list:
                ctr_prov_ctx = \
                    self.ovdc_cache.get_ovdc_container_provider_metadata(
                        ovdc_name=vdc['name'], org_name=org.get_name(),
                        credentials_required=False)
                vdc_dict = {
                    'org': org.get_name(),
                    'name': vdc['name'],
                    CONTAINER_PROVIDER_KEY:
                        ctr_prov_ctx[CONTAINER_PROVIDER_KEY]
                }
                ovdc_list.append(vdc_dict)
        return ovdc_list

    def _get_cluster_info(self, **cluster_spec):
        """Get cluster details directly from cloud provider.

        Logic of the method is as follows.

        If 'ovdc' is present in the cluster spec,
            choose the right broker (by identifying the container_provider
            (vcd|pks) defined for that ovdc) to do get_cluster operation.
        else
            Invoke set of all (vCD/PKS) brokers in the org to find the cluster

        :return: a tuple of cluster information as dictionary and the broker
            instance used to find the cluster information.

        :rtype: tuple
        """
        cluster_name = cluster_spec['cluster_name']
        if self.is_ovdc_present_in_request:
            broker = self.get_broker_based_on_vdc()
            return broker.get_cluster_info(cluster_name=cluster_name), broker
        else:
            cluster, broker = self._find_cluster_in_org(cluster_name)
            if cluster:
                return cluster, broker

        raise ClusterNotFoundError(f'cluster {cluster_name} not found '
                                   f'either in vCD or PKS')

    def _get_cluster_config(self, **cluster_spec):
        """Get the cluster configuration.

        :param str cluster_name: Name of cluster.

        :return: Cluster config.

        :rtype: str
        """
        cluster_name = cluster_spec['cluster_name']
        if self.is_ovdc_present_in_request:
            broker = self.get_broker_based_on_vdc()
            return broker.get_cluster_config(cluster_name=cluster_name)
        else:
            cluster, broker = self._find_cluster_in_org(cluster_name)
            if cluster:
                return broker.get_cluster_config(cluster_name=cluster['name'])

        raise ClusterNotFoundError(f'cluster {cluster_name} not found '
                                   f'either in vCD or PKS')

    def _list_clusters(self):
        """Logic of the method is as follows.

        If 'ovdc' is present in the body,
            choose the right broker (by identifying the container_provider
            (vcd|pks) defined for that ovdc) to do list_clusters operation.
        Else
            Invoke set of all (vCD/PKS)brokers in the org to do list_clusters.
            Post-process the result returned by each broker.
            Aggregate all the results into one.
        """
        if self.is_ovdc_present_in_request:
            broker = self.get_broker_based_on_vdc()
            return broker.list_clusters()
        else:
            common_cluster_properties = ('name', 'vdc', 'status')
            vcd_broker = VcdBroker(self.req_headers, self.req_spec)
            vcd_clusters = []
            for cluster in vcd_broker.list_clusters():
                vcd_cluster = {k: cluster.get(k, None) for k in
                               common_cluster_properties}
                vcd_cluster[CONTAINER_PROVIDER_KEY] = CtrProvType.VCD.value
                vcd_clusters.append(vcd_cluster)

            pks_clusters = []
            pks_ctx_list = self._create_pks_context_for_all_accounts_in_org()
            for pks_ctx in pks_ctx_list:
                pks_broker = PKSBroker(self.req_headers, self.req_spec,
                                       pks_ctx)
                for cluster in pks_broker.list_clusters():
                    pks_cluster = self._get_truncated_cluster_info(
                        cluster, pks_broker, common_cluster_properties)
                    pks_cluster[CONTAINER_PROVIDER_KEY] = CtrProvType.PKS.value
                    pks_clusters.append(pks_cluster)
            return vcd_clusters + pks_clusters

    def _resize_cluster(self, **cluster_spec):
        cluster, broker = self._get_cluster_info(**cluster_spec)
        return broker.resize_cluster(curr_cluster_info=cluster, **cluster_spec)

    def _delete_cluster(self, **cluster_spec):
        cluster_name = cluster_spec['cluster_name']
        _, broker = self._get_cluster_info(**cluster_spec)
        return broker.delete_cluster(cluster_name=cluster_name)

    def _create_cluster(self, **cluster_spec):
        cluster_name = cluster_spec['cluster_name']
        cluster = self._find_cluster_in_org(cluster_name)[0]
        if not cluster:
            broker = self.get_broker_based_on_vdc()
            return broker.create_cluster(**cluster_spec)
        else:
            raise CseServerError(f'Cluster with name: {cluster_name} '
                                 f'already found')

    def _find_cluster_in_org(self, cluster_name):
        """Invoke set of all (vCD/PKS)brokers in the org to find the cluster.

        If cluster found:
            Return a tuple of (cluster and the broker instance used to find
            the cluster)
        Else:
            (None, None) if cluster not found.
        """
        vcd_broker = VcdBroker(self.req_headers, self.req_spec)
        try:
            return vcd_broker.get_cluster_info(cluster_name), vcd_broker
        except Exception as err:
            LOGGER.debug(f"Get cluster info on {cluster_name} failed "
                         f"on vCD with error: {err}")

        pks_ctx_list = self._create_pks_context_for_all_accounts_in_org()
        for pks_ctx in pks_ctx_list:
            pksbroker = PKSBroker(self.req_headers, self.req_spec, pks_ctx)
            try:
                return pksbroker.get_cluster_info(cluster_name=cluster_name),\
                    pksbroker
            except Exception as err:
                LOGGER.debug(f"Get cluster info on {cluster_name} failed "
                             f"on {pks_ctx['host']} with error: {err}")

        return None, None

    def _create_pks_context_for_all_accounts_in_org(self):
        """Create PKS context for accounts in a given Org.

        If user is Sysadmin
            Creates PKS contexts for all PKS accounts defined in the entire
            system.
        else
            Creates PKS contexts for all PKS accounts assigned to the org.
            However if separate service accounts for each org hasn't been
            configued by admin via pks.yaml, then PKS accounts of the PKS
            server corresponding to the vCenters powering the individual
            orgVDC of the org will be picked up for creating the PKS contexts.

        :return: list of dict, where each dictionary is a PKS context

        :rtype: list
        """
        if not self.pks_cache:
            return []

        if self.vcd_client.is_sysadmin():
            all_pks_account_info = \
                self.pks_cache.get_all_pks_account_info_in_system()
            pks_ctx_list = [OvdcCache.construct_pks_context(
                pks_account_info, credentials_required=True)
                for pks_account_info in all_pks_account_info]
            return pks_ctx_list

        org_name = self.session.get('org')
        if self.pks_cache.do_orgs_have_exclusive_pks_account():
            pks_account_infos = \
                self.pks_cache.get_exclusive_pks_accounts_info_for_org(
                    org_name)
            pks_ctx_list = [OvdcCache.construct_pks_context
                            (pks_account_info, credentials_required=True)
                            for pks_account_info in pks_account_infos]
        else:
            org_resource = self.vcd_client.get_org()
            org = Org(self.vcd_client, resource=org_resource)
            vdc_names = [vdc['name'] for vdc in org.list_vdcs()]
            # Constructing dict instead of list to avoid duplicates
            # TODO() figure out a way to add pks contexts to a set directly
            pks_ctx_dict = {}
            for vdc_name in vdc_names:
                # this is a full blown pks_account_info + pvdc_info +
                # compute_profile_name dictionary
                ctr_prov_ctx = \
                    self.ovdc_cache.get_ovdc_container_provider_metadata(
                        ovdc_name=vdc_name, org_name=org_name,
                        credentials_required=True)
                if ctr_prov_ctx[CONTAINER_PROVIDER_KEY] == \
                        CtrProvType.PKS.value:
                    pks_ctx_dict[ctr_prov_ctx['vc']] = ctr_prov_ctx

            pks_ctx_list = list(pks_ctx_dict.values())

        return pks_ctx_list

    def _get_truncated_cluster_info(self, cluster, pks_broker,
                                    cluster_property_keys):
        pks_cluster = {k: cluster.get(k) for k in
                       cluster_property_keys}
        # Extract vdc name from compute-profile-name
        # Example: vdc name in the below compute profile is: vdc-PKS1
        # compute-profile: cp--f3272127-9b7f-4f90-8849-0ee70a28be56--vdc-PKS1
        compute_profile_name = cluster.get('compute-profile-name', '')
        pks_cluster['vdc'] = compute_profile_name.split('--')[-1] \
            if compute_profile_name else ''
        pks_cluster['status'] = \
            cluster.get('last-action', '').lower() + ' ' + \
            pks_cluster.get('status', '').lower()
        return pks_cluster

    def get_broker_based_on_vdc(self):
        """Get the broker based on ovdc.

        :return: broker

        :rtype: container_service_extension.abstract_broker.AbstractBroker
        """
        ovdc_name = self.req_spec.get('vdc') or \
            self.req_qparams.get('vdc')
        org_name = self.req_spec.get('org') or \
            self.req_qparams.get('org') or \
            self.session.get('org')

        LOGGER.debug(f"org_name={org_name};vdc_name=\'{ovdc_name}\'")

        # Get the ovdc metadata using org and ovdc.
        # Create the right broker based on value of 'container_provider'.
        # Fall back to DefaultBroker for missing ovdc or org.
        if ovdc_name and org_name:
            ctr_prov_ctx = \
                self.ovdc_cache.get_ovdc_container_provider_metadata(
                    ovdc_name=ovdc_name, org_name=org_name,
                    credentials_required=True, nsxt_info_required=True)
            LOGGER.debug(
                f"ovdc metadata for {ovdc_name}-{org_name}=>{ctr_prov_ctx}")
            if ctr_prov_ctx.get(CONTAINER_PROVIDER_KEY) == \
                    CtrProvType.PKS.value:
                return PKSBroker(self.req_headers, self.req_spec,
                                 pks_ctx=ctr_prov_ctx)
            elif ctr_prov_ctx.get(CONTAINER_PROVIDER_KEY) == \
                    CtrProvType.VCD.value:
                return VcdBroker(self.req_headers, self.req_spec)
            else:
                raise CseServerError(f"Vdc '{ovdc_name}' is not enabled for "
                                     "Kubernetes cluster deployment")
        else:
            # TODO() - This call should be based on a boolean flag
            # Specify flag in config file whether to have default
            # handling is required for missing ovdc or org.
            return VcdBroker(self.req_headers, self.req_spec)

    def _get_ovdc_params(self):
        ovdc_id = self.req_spec.get('ovdc_id')
        org_name = self.req_spec.get('org_name')
        pks_plans = self.req_spec['pks_plans']
        ovdc = self.ovdc_cache.get_ovdc(ovdc_id=ovdc_id)
        pvdc_id = self.ovdc_cache.get_pvdc_id(ovdc)

        pks_context = None
        if self.req_spec[CONTAINER_PROVIDER_KEY] == CtrProvType.PKS.value:
            if not self.pks_cache:
                raise CseServerError('PKS config file does not exist')
            pvdc_info = self.pks_cache.get_pvdc_info(pvdc_id)
            pks_account_info = self.pks_cache.get_pks_account_info(
                org_name, pvdc_info.vc)
            nsxt_info = self.pks_cache.get_nsxt_info(pvdc_info.vc)

            pks_compute_profile_name = \
                self.ovdc_cache.get_compute_profile_name(
                    ovdc_id, ovdc.resource.get('name'))
            pks_context = OvdcCache.construct_pks_context(
                pks_account_info=pks_account_info,
                pvdc_info=pvdc_info,
                nsxt_info=nsxt_info,
                pks_compute_profile_name=pks_compute_profile_name,
                pks_plans=pks_plans,
                credentials_required=True)

        return pks_context, ovdc

    def _create_pks_compute_profile(self, pks_ctx):
        ovdc_id = self.req_spec.get('ovdc_id')
        org_name = self.req_spec.get('org_name')
        ovdc_name = self.req_spec.get('ovdc_name')
        # Compute profile creation
        pks_compute_profile_name = self.\
            ovdc_cache.get_compute_profile_name(ovdc_id, ovdc_name)
        pks_compute_profile_description = f"{org_name}--{ovdc_name}" \
            f"--{ovdc_id}"
        pks_az_name = f"az-{ovdc_name}"
        ovdc_rp_name = f"{ovdc_name} ({ovdc_id})"

        compute_profile_params = PksComputeProfileParams(
            pks_compute_profile_name, pks_az_name,
            pks_compute_profile_description,
            pks_ctx.get('cpi'),
            pks_ctx.get('datacenter'),
            pks_ctx.get('cluster'),
            ovdc_rp_name).to_dict()

        LOGGER.debug(f"Creating PKS Compute Profile with name:"
                     f"{pks_compute_profile_name}")

        pksbroker = PKSBroker(self.req_headers, self.req_spec, pks_ctx)
        try:
            pksbroker.create_compute_profile(**compute_profile_params)
        except PksServerError as ex:
            if ex.status == HTTPStatus.CONFLICT.value:
                LOGGER.debug(f"Compute profile name {pks_compute_profile_name}"
                             f" already exists\n{str(ex)}")
            else:
                raise ex


class PksComputeProfileParams(namedtuple("PksComputeProfileParams",
                                         'cp_name, az_name, description,'
                                         'cpi,datacenter_name, '
                                         'cluster_name, ovdc_rp_name')):
    """Construct PKS ComputeProfile Parameters ."""

    def __str__(self):
        return f"class:{PksComputeProfileParams.__name__}," \
            f" cp_name:{self.cp_name}, az_name:{self.az_name}, " \
            f" description:{self.description}, cpi:{self.cpi}, " \
            f" datacenter_name:{self.datacenter_name}, " \
            f" cluster_name:{self.cluster_name}, " \
            f" ovdc_rp_name:{self.ovdc_rp_name}"

    def to_dict(self):
        return dict(self._asdict())
