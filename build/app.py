import kopf
import os
import time
import sys
import kubernetes.client
from kubernetes import config
from kubernetes.client.rest import ApiException

# Global set to keep track of monitored namespaces
namespace_monitors = set()
exempt_namespaces = set()

def trigger_rollout(name, namespace):
    """
    Triggers a rollout of a deployment by updating an annotation.
    """
    apps_v1_api = kubernetes.client.AppsV1Api()
    deployment = apps_v1_api.read_namespaced_deployment(name, namespace)
    if 'annotations' not in deployment.spec.template.metadata:
        deployment.spec.template.metadata.annotations = {}
    # Use a timestamp or a counter for the annotation value to ensure it changes.
    deployment.spec.template.metadata.annotations['vpa-update-timestamp'] = str(time.time())
    try:
        apps_v1_api.patch_namespaced_deployment(name, namespace, deployment)
        print(f"Triggered rollout for deployment {name} in namespace {namespace}.")
    except ApiException as e:
        print(f"Exception when calling AppsV1Api->patch_namespaced_deployment: {e}")

def create_vpa(name, namespace, update_mode):
    """
    Creates or updates a VPA with the specified update mode.
    """
    vpa_api = kubernetes.client.CustomObjectsApi()
    vpa_body = {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": {
            "name": name,
            "namespace": namespace
        },
        "spec": {
            "targetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": name
            },
            "updatePolicy": {
                "updateMode": update_mode
            }
        }
    }
    # Try to update an existing VPA, if it doesn't exist, create a new one.
    try:
        vpa_api.patch_namespaced_custom_object(
            group="autoscaling.k8s.io",
            version="v1",
            namespace=namespace,
            plural="verticalpodautoscalers",
            name=name,
            body=vpa_body,
        )
        print(f"VPA updated for deployment {name} in namespace {namespace}.")
    except ApiException as e:
        if e.status == 404:  # VPA doesn't exist, try creating it
            try:
                vpa_api.create_namespaced_custom_object(
                    group="autoscaling.k8s.io",
                    version="v1",
                    namespace=namespace,
                    plural="verticalpodautoscalers",
                    body=vpa_body,
                )
                print(f"VPA created for deployment {name} in namespace {namespace}.")
            except ApiException as e:
                print(f"Exception when creating VPA: {e}")
        else:
            print(f"Exception when updating VPA: {e}")

def get_rollout_strategy(namespace, deployment_name):
    """
    Retrieves the rollout strategy for the specified namespace and deployment.
    """
    custom_objects_api = kubernetes.client.CustomObjectsApi()
    try:
        rollout_strategies = custom_objects_api.list_namespaced_custom_object(
            group="asalaboratory.com",
            version="v1",
            namespace=namespace,
            plural="rolloutstrategies"
        )
        for strategy in rollout_strategies.get("items", []):
            if strategy.get("spec", {}).get("target") == deployment_name or not strategy.get("spec", {}).get("target"):
                return strategy.get("spec", {}).get("strategy")
    except ApiException as e:
        print(f"Failed to retrieve rollout strategies: {e}")
    return None  # Return None if no specific strategy is found

def check_vpa_installed(api_instance, vpa_crds, retries=3, initial_delay=1):
    """
    Check if the specified VPA CRDs are installed.

    Parameters:
    - api_instance: An instance of the CustomObjectsApi from the Kubernetes client.
    - vpa_crds: A list of VPA CRD names to check.
    - retries: Maximum number of retries for checking each CRD.
    - initial_delay: Initial delay between retries, doubled after each attempt.
    """
    for crd_name in vpa_crds:
        attempt = 0
        delay = initial_delay
        while attempt < retries:
            try:
                api_response = api_instance.read_custom_resource_definition(crd_name)
                print(f"Found CRD: {crd_name}")
                break  # Successfully found the CRD, break out of the retry loop
            except ApiException as e:
                if e.status == 404:
                    print(f"CRD {crd_name} not found. VPA operator might not be installed.")
                    if attempt == retries - 1:  # Last attempt
                        print("Required VPA operator not found. Exiting...")
                        sys.exit(1)
                else:
                    print(f"Attempt {attempt + 1} failed: Error checking CRD {crd_name}: {e}")
                time.sleep(delay)
                delay *= 2  # Exponential backoff
                attempt += 1

@kopf.on.create('asalaboratory.com', 'v1', 'namespacemonitors')
def on_namespace_monitor_create(spec, **kwargs):
    namespace = spec.get('namespace')
    if namespace:
        namespace_monitors.add(namespace)
        print(f"Added {namespace} to monitored namespaces.")

@kopf.on.delete('asalaboratory.com', 'v1', 'namespacemonitors')
def on_namespace_monitor_delete(spec, **kwargs):
    namespace = spec.get('namespace')
    if namespace in namespace_monitors:
        namespace_monitors.remove(namespace)
        print(f"Removed {namespace} from monitored namespaces.")

@kopf.on.create('asalaboratory.com', 'v1', 'exemptnamespaces')
def on_exempt_namespace_create(spec, **kwargs):
    namespace = spec.get('namespace')
    if namespace:
        exempt_namespaces.add(namespace)
        print(f"Added {namespace} to exempt namespaces.")

@kopf.on.delete('asalaboratory.com', 'v1', 'exemptnamespaces')
def on_exempt_namespace_delete(spec, **kwargs):
    namespace = spec.get('namespace')
    if namespace in exempt_namespaces:
        exempt_namespaces.remove(namespace)
        print(f"Removed {namespace} from exempt namespaces.")

@kopf.on.create('apps', 'v1', 'deployments')
def on_deployment_create(namespace, name, spec, **kwargs):
    # Check if namespace is monitored and not exempt
    if (not namespace_monitors or namespace in namespace_monitors) and (namespace not in exempt_namespaces):
        print(f"Deployment created in monitored namespace: {namespace}. Creating VPA...")
        strategy = get_rollout_strategy(namespace, name) or "Auto"  # Default to Auto if not specified
        create_vpa(name, namespace, strategy)
        trigger_rollout(name, namespace)
    else:
        print(f"Namespace {namespace} is exempt or not monitored. Ignoring deployment.")

@kopf.on.delete('apps', 'v1', 'deployments')
def on_deployment_delete(name, namespace, **kwargs):
    """
    Reacts to deployment deletions and deletes the corresponding VPA.
    """
    if (not namespace_monitors or namespace in namespace_monitors) and (namespace not in exempt_namespaces):
        print(f"Deployment deleted in monitored namespace: {namespace}. Deleting VPA...")
        delete_vpa(name, namespace)
    else:
        print(f"Namespace {namespace} is exempt or not monitored. Ignoring deployment.")

def delete_vpa(name, namespace):
    """
    Deletes a VPA for a given deployment.
    """
    vpa_api = kubernetes.client.CustomObjectsApi()

    try:
        # Delete VPA
        vpa_api.delete_namespaced_custom_object(
            group="autoscaling.k8s.io",
            version="v1",
            namespace=namespace,
            plural="verticalpodautoscalers",
            name=name,
        )
        print(f"VPA deleted for deployment {name} in namespace {namespace}.")
    except ApiException as e:
        print(f"Exception when calling CustomObjectsApi->delete_namespaced_custom_object: {e}")

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.watching.server_timeout = 60
    settings.watching.client_timeout = 60

def get_namespaces_from_crs(crs):
    """Extract namespaces from CRs."""
    return {cr['spec']['namespace'] for cr in crs['items'] if 'namespace' in cr['spec']}

@kopf.on.startup()
def on_startup(**_):
    config.load_incluster_config()
    api_instance = kubernetes.client.CustomObjectsApi()
    v1 = kubernetes.client.CoreV1Api()

    default_namespaces_file = 'default_namespaces'
    default_namespaces = set()

    api_ext = kubernetes.client.ApiextensionsV1Api()

    vpa_crds = ['verticalpodautoscalers.autoscaling.k8s.io', 'verticalpodautoscalercheckpoints.autoscaling.k8s.io']

    check_vpa_installed(api_ext, vpa_crds)

    try:
        with open(default_namespaces_file, 'r') as file:
            for line in file:
                default_namespaces.add(line.strip())
    except FileNotFoundError:
        print(f"File {default_namespaces_file} not found. No namespaces will be excluded.")
    except Exception as e:
        print(f"Error reading {default_namespaces_file}: {e}")

    print(f"Excluded namespaces: {default_namespaces}")

    try:
        namespace_monitor_crs = api_instance.list_cluster_custom_object(group="asalaboratory.com", version="v1", plural="namespacemonitors")
        exempt_namespace_crs = api_instance.list_cluster_custom_object(group="asalaboratory.com", version="v1", plural="exemptnamespaces")
        
        namespace_monitors = get_namespaces_from_crs(namespace_monitor_crs)
        exempt_namespaces = get_namespaces_from_crs(exempt_namespace_crs)

        print(f"Monitored Namespaces: {namespace_monitors}")

        for namespace in default_namespaces():
            if namespace:
                print(f"Operator's namespace {oper_namespace} added to exempt namespaces.")
                exempt_namespaces.add(namespace)
            else:
                print("Operator's namespace could not be determined.")

        namespaces = v1.list_namespace().items
        initial_namespaces = {ns.metadata.name for ns in namespaces if ns.metadata.name not in default_namespaces}

        # Get current namespace and add to exempt namespaces
        oper_namespace = os.getenv('OPERATOR_NAMESPACE')
        if (not namespace_monitors or oper_namespace in namespace_monitors) and (oper_namespace not in exempt_namespaces):
            if oper_namespace:
                exempt_namespaces.add(oper_namespace)
                print(f"Operator's namespace {oper_namespace} added to exempt namespaces.")
            else:
                print("Operator's namespace could not be determined.")

        for namespace in initial_namespaces:
            if (not namespace_monitors or namespace in namespace_monitors) and (namespace not in exempt_namespaces):
                print(f"Namespace {namespace} will be monitored.")
            else:
                print(f"Namespace {namespace} is exempt or not selected for monitoring.")
        
        print(f"Final Default Exempt Namespaces: {exempt_namespaces}")

    except ApiException as e:
        if e.status == 503:
            print("Kubernetes API is temporarily unavailable. Please check your cluster status.")
        else:
            print(f"An error occurred: {e}")
