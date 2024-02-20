import kopf
import time
import sys
import kubernetes.client
from kubernetes import config
from kubernetes.client.rest import ApiException

# Global set to keep track of monitored namespaces
namespace_monitors = set()
exempt_namespaces = set()

def create_vpa(name, namespace):
    """
    Creates a VPA for a given deployment.
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
                "updateMode": "Auto"
            }
        }
    }

    try:
        # Create VPA
        vpa_api.create_namespaced_custom_object(
            group="autoscaling.k8s.io",
            version="v1",
            namespace=namespace,
            plural="verticalpodautoscalers",
            body=vpa_body,
        )
        print(f"VPA created for deployment {name} in namespace {namespace}.")
    except ApiException as e:
        print(f"Exception when calling CustomObjectsApi->create_namespaced_custom_object: {e}")

def check_vpa_installed(api_instance, vpa_crds, retries=3, delay=1):
    vpa_installed = True
    for crd_name in vpa_crds:
        attempt = 0
        while attempt <= retries:
            try:
                api_response = api_instance.read_custom_resource_definition(crd_name)
                print(f"Found CRD: {crd_name}")
                break  # Successfully found the CRD, break out of the retry loop
            except ApiException as e:
                if e.status == 404:
                    print(f"CRD {crd_name} not found. VPA operator might not be installed.")
                    vpa_installed = False
                    break  # No need to retry if CRD is not found
                else:
                    print(f"Attempt {attempt} failed: Error checking CRD {crd_name}: {e}")
                    if attempt == retries:
                        print("Max retries reached. Exiting...")
                        vpa_installed = False
                        break
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                    attempt += 1

    if not vpa_installed:
        print("Required VPA operator not found. Exiting to retry...")
        sys.exit(1)

@kopf.on.create('mydomain.org', 'v1', 'namespacemonitors')
def on_namespace_monitor_create(spec, **kwargs):
    namespace = spec.get('namespace')
    if namespace:
        namespace_monitors.add(namespace)
        print(f"Added {namespace} to monitored namespaces.")

@kopf.on.delete('mydomain.org', 'v1', 'namespacemonitors')
def on_namespace_monitor_delete(spec, **kwargs):
    namespace = spec.get('namespace')
    if namespace in namespace_monitors:
        namespace_monitors.remove(namespace)
        print(f"Removed {namespace} from monitored namespaces.")

@kopf.on.create('mydomain.org', 'v1', 'exemptnamespaces')
def on_exempt_namespace_create(spec, **kwargs):
    namespace = spec.get('namespace')
    if namespace:
        exempt_namespaces.add(namespace)
        print(f"Added {namespace} to exempt namespaces.")

@kopf.on.delete('mydomain.org', 'v1', 'exemptnamespaces')
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
        create_vpa(name, namespace)
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
    config.load_incluster_config()  # Adjust this line as per your running environment
    api_instance = kubernetes.client.CustomObjectsApi()
    v1 = kubernetes.client.CoreV1Api()

    default_namespaces_file = 'default_namespaces'
    default_namespaces = set()

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
        namespace_monitor_crs = api_instance.list_cluster_custom_object(group="mydomain.org", version="v1", plural="namespacemonitors")
        exempt_namespace_crs = api_instance.list_cluster_custom_object(group="mydomain.org", version="v1", plural="exemptnamespaces")
        
        namespace_monitors = get_namespaces_from_crs(namespace_monitor_crs)
        exempt_namespaces = get_namespaces_from_crs(exempt_namespace_crs)

        print(f"Monitored Namespaces: {namespace_monitors}")
        print(f"Exempt Namespaces: {exempt_namespaces}")

        namespaces = v1.list_namespace().items
        initial_namespaces = {ns.metadata.name for ns in namespaces if ns.metadata.name not in default_namespaces}

        for namespace in initial_namespaces:
            if (not namespace_monitors or namespace in namespace_monitors) and (namespace not in exempt_namespaces):
                print(f"Namespace {namespace} will be monitored.")
            else:
                print(f"Namespace {namespace} is exempt or not selected for monitoring.")

    except ApiException as e:
        if e.status == 503:
            print("Kubernetes API is temporarily unavailable. Please check your cluster status.")
        else:
            print(f"An error occurred: {e}")
