"""
DWE Superset Infrastructure — Azure Pulumi IaC
Provisions: App Gateway (HTTP→HTTPS) + VMSS + DNS A record

Single VMSS runs Superset app + Celery workers via Docker Compose.
Stateless — VMSS can be reimaged freely; database and Redis are external.
Run with: pulumi stack select prod && pulumi up --yes
"""

import base64
import json
from pathlib import Path

import pulumi
import pulumi_azure_native as azure_native
import pulumi_azure_native.dbforpostgresql.v20221201 as pg
from pulumi_azure_native.cache import Redis as AzureRedis, SkuArgs as RedisSkuArgs
import yaml
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

# ─────────────────────────────────────────────────────────────────────────────
# Hydration config — written by dwe-core at create-service / update-service time
# ─────────────────────────────────────────────────────────────────────────────
_hydration = Path(__file__).parent / "dwe-hydration.yaml"
_dwe         = yaml.safe_load(_hydration.read_text()) if _hydration.exists() else {}
if _dwe:
    project_name    = _dwe["project_name"]
    git_repo_url    = _dwe["git_repo_url"]
    adapter_version = _dwe["adapter_version"]
else:
    _cfg         = pulumi.Config()
    project_name    = _cfg.get("project_name") or pulumi.get_project()
    git_repo_url    = _cfg.get("git_repo_url") or ""
    adapter_version = _cfg.get("adapter_version") or "v1.0.0"

# ─────────────────────────────────────────────────────────────────────────────
# Stack Config
# ─────────────────────────────────────────────────────────────────────────────
config = pulumi.Config()
env                  = config.require("environment")
git_branch           = config.require("git_branch")
secret_id            = config.require("secret_id")
key_vault_name       = config.require("key_vault_name")
azure_location       = config.get("azure_location") or "eastus"
vm_size              = config.get("instance_type") or "Standard_D4s_v3"
volume_size          = int(config.get("volume_size") or "50")
resource_group       = config.require("resource_group")
subscription_id      = config.require("subscription_id")
startup_code_version = config.get("startup_code_version") or ""
redis_mode           = config.get("redis_mode") or "local"
redis_sku_name       = config.get("redis_sku") or "C0"
app_port             = 8088

suffix = f"-{env}" if env != "prod" else ""
tags = {
    "Project":     project_name,
    "ManagedBy":   "Pulumi",
    "Environment": env,
    "GitBranch":   git_branch,
}

# ─────────────────────────────────────────────────────────────────────────────
# Load secrets from Azure Key Vault
# ─────────────────────────────────────────────────────────────────────────────
def get_secret(kv_name: str, sid: str) -> dict:
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=f"https://{kv_name}.vault.azure.net/", credential=credential)
    secret = client.get_secret(sid)
    return json.loads(secret.value)

secrets = get_secret(key_vault_name, secret_id)

# ── Infrastructure ────────────────────────────────────────────────────────────
vnet_id          = secrets["VNET_ID"]
common_app_gw_id        = secrets.get("COMMON_APP_GW_ID", "")
common_app_gw_public_ip = secrets.get("COMMON_APP_GW_PUBLIC_IP", "")
use_common_lb = bool(common_app_gw_id)
if use_common_lb:
    if not common_app_gw_public_ip:
        raise ValueError("COMMON_APP_GW_PUBLIC_IP is required when COMMON_APP_GW_ID is set")
else:
    app_gw_subnet_id = secrets["APP_GW_SUBNET_ID"]
vm_subnet_id     = secrets["VM_SUBNET_ID"]
ssh_public_key   = secrets["SSH_PUBLIC_KEY"]

# ── Networking / DNS ──────────────────────────────────────────────────────────
dns_zone_name   = secrets["DNS_ZONE_NAME"]
dns_record_name = secrets["DNS_RECORD_NAME"]
dns_zone_rg     = secrets.get("DNS_ZONE_RESOURCE_GROUP", resource_group)
ssl_cert_kv_id  = secrets.get("APP_GW_SSL_CERT_KEY_VAULT_ID", "")

# ── Git ───────────────────────────────────────────────────────────────────────
git_deploy_token    = secrets["git_deploy_token"]
git_deploy_username = secrets.get("git_deploy_username", "x-token-auth")

# ── Database — created in existing PostgreSQL Flexible Server ─────────────────
db_host = secrets["DB_HOST"]
db_pass = secrets["DB_PASS"]
db_user = secrets.get("DB_USER", "superset")
db_port = secrets.get("DB_PORT", "5432")
db_name = f"superset_{env}"
sqlalchemy_database_uri = f"postgresql+psycopg2://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"

# ─────────────────────────────────────────────────────────────────────────────
# User-assigned Managed Identity (used by VMSS to read Key Vault)
# ─────────────────────────────────────────────────────────────────────────────
identity = azure_native.managedidentity.UserAssignedIdentity(
    f"{project_name}-identity{suffix}",
    resource_group_name=resource_group,
    location=azure_location,
    resource_name_=f"{project_name}-identity{suffix}",
    tags=tags,
)

kv_access = azure_native.authorization.RoleAssignment(
    f"{project_name}-kv-role{suffix}",
    scope=pulumi.Output.format(
        "/subscriptions/{0}/resourceGroups/{1}/providers/Microsoft.KeyVault/vaults/{2}",
        subscription_id, resource_group, key_vault_name,
    ),
    role_definition_id=pulumi.Output.format(
        "/subscriptions/{0}/providers/Microsoft.Authorization/roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6",
        subscription_id,
    ),
    principal_id=identity.principal_id,
    principal_type="ServicePrincipal",
)

# ── PostgreSQL database — created in existing cluster ─────────────────────────
pg.Database(
    f"{project_name}-pg-db{suffix}",
    resource_group_name=resource_group,
    server_name=db_host.split(".")[0],
    database_name=db_name,
)

# ── Redis ─────────────────────────────────────────────────────────────────────
# redis_mode=local  → Redis container on the VM (free, no SLA)
# redis_mode=managed → Azure Cache for Redis (redis_sku: C0 Basic ~$16/mo, C1 Standard ~$60/mo)
if redis_mode == "managed":
    _sku_capacity = int(redis_sku_name[1])
    _sku_tier = "Basic" if _sku_capacity == 0 else "Standard"
    _redis_cache = AzureRedis(
        f"{project_name}-redis{suffix}",
        resource_group_name=resource_group,
        location=azure_location,
        name=f"{project_name}-redis{suffix}",
        sku=RedisSkuArgs(name=_sku_tier, family="C", capacity=_sku_capacity),
        enable_non_ssl_port=True,
        minimum_tls_version="1.0",
        tags=tags,
    )
    _redis_host_output = _redis_cache.host_name
    _redis_port = "6379"
else:
    _redis_host_output = pulumi.Output.from_input("redis")
    _redis_port = "6379"

# ─────────────────────────────────────────────────────────────────────────────
# Public IP for Application Gateway (skipped when using existing load balancer)
# ─────────────────────────────────────────────────────────────────────────────
public_ip = None
if not use_common_lb:
    public_ip = azure_native.network.PublicIPAddress(
        f"{project_name}-pip{suffix}",
        resource_group_name=resource_group,
        location=azure_location,
        public_ip_address_name=f"{project_name}-pip{suffix}",
        sku=azure_native.network.PublicIPAddressSkuArgs(name="Standard"),
        public_ip_allocation_method="Static",
        tags=tags,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Network Security Group
# ─────────────────────────────────────────────────────────────────────────────
vm_nsg = azure_native.network.NetworkSecurityGroup(
    f"{project_name}-vm-nsg{suffix}",
    resource_group_name=resource_group,
    location=azure_location,
    network_security_group_name=f"{project_name}-vm-nsg{suffix}",
    security_rules=[
        azure_native.network.SecurityRuleArgs(
            name="AllowSuperset",
            priority=100, direction="Inbound", access="Allow", protocol="Tcp",
            source_port_range="*", destination_port_range=str(app_port),
            source_address_prefix="VirtualNetwork", destination_address_prefix="*",
        ),
        azure_native.network.SecurityRuleArgs(
            name="AllowSSH",
            priority=110, direction="Inbound", access="Allow", protocol="Tcp",
            source_port_range="*", destination_port_range="22",
            source_address_prefix="VirtualNetwork", destination_address_prefix="*",
        ),
    ],
    tags=tags,
)

# ─────────────────────────────────────────────────────────────────────────────
# Application Gateway (HTTP→HTTPS redirect + HTTPS→Superset backend)
# ─────────────────────────────────────────────────────────────────────────────
app_gw_name = f"{project_name}-appgw{suffix}"
ag_prefix = (
    f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    f"/providers/Microsoft.Network/applicationGateways/{app_gw_name}"
)

ssl_certs = []
if ssl_cert_kv_id:
    ssl_certs = [azure_native.network.ApplicationGatewaySslCertificateArgs(
        name="tls-cert",
        key_vault_secret_id=ssl_cert_kv_id,
    )]

has_ssl = bool(ssl_certs)

http_listeners = [
    azure_native.network.ApplicationGatewayHttpListenerArgs(
        name="http-listener",
        frontend_ip_configuration=azure_native.network.SubResourceArgs(
            id=f"{ag_prefix}/frontendIPConfigurations/appGwPublicFrontendIp"),
        frontend_port=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/frontendPorts/port_80"),
        protocol="Http",
    ),
]

redirect_configurations = []
if has_ssl:
    http_listeners.append(azure_native.network.ApplicationGatewayHttpListenerArgs(
        name="https-listener",
        frontend_ip_configuration=azure_native.network.SubResourceArgs(
            id=f"{ag_prefix}/frontendIPConfigurations/appGwPublicFrontendIp"),
        frontend_port=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/frontendPorts/port_443"),
        protocol="Https",
        ssl_certificate=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/sslCertificates/tls-cert"),
    ))
    redirect_configurations = [azure_native.network.ApplicationGatewayRedirectConfigurationArgs(
        name="redirect-to-https",
        redirect_type="Permanent",
        target_listener=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/httpListeners/https-listener"),
        include_path=True,
        include_query_string=True,
    )]
    routing_rules = [
        azure_native.network.ApplicationGatewayRequestRoutingRuleArgs(
            name="http-redirect",
            priority=10, rule_type="Basic",
            http_listener=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/httpListeners/http-listener"),
            redirect_configuration=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/redirectConfigurations/redirect-to-https"),
        ),
        azure_native.network.ApplicationGatewayRequestRoutingRuleArgs(
            name="https-route",
            priority=20, rule_type="Basic",
            http_listener=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/httpListeners/https-listener"),
            backend_address_pool=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/backendAddressPools/backendPool"),
            backend_http_settings=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/backendHttpSettingsCollection/backendHttpSettings"),
        ),
    ]
else:
    routing_rules = [
        azure_native.network.ApplicationGatewayRequestRoutingRuleArgs(
            name="http-route",
            priority=10, rule_type="Basic",
            http_listener=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/httpListeners/http-listener"),
            backend_address_pool=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/backendAddressPools/backendPool"),
            backend_http_settings=azure_native.network.SubResourceArgs(
                id=f"{ag_prefix}/backendHttpSettingsCollection/backendHttpSettings"),
        )
    ]

ag_identity = identity.id.apply(lambda iid: azure_native.network.ManagedServiceIdentityArgs(
    type="UserAssigned",
    user_assigned_identities={iid: {}},
)) if has_ssl else None

app_gw = None
if not use_common_lb:
    app_gw = azure_native.network.ApplicationGateway(
        app_gw_name,
        resource_group_name=resource_group,
        application_gateway_name=app_gw_name,
        location=azure_location,
        sku=azure_native.network.ApplicationGatewaySkuArgs(name="Standard_v2", tier="Standard_v2", capacity=1),
        identity=ag_identity,
        gateway_ip_configurations=[azure_native.network.ApplicationGatewayIPConfigurationArgs(
            name="appGatewayIpConfig",
            subnet=azure_native.network.SubResourceArgs(id=app_gw_subnet_id),
        )],
        frontend_ip_configurations=[azure_native.network.ApplicationGatewayFrontendIPConfigurationArgs(
            name="appGwPublicFrontendIp",
            public_ip_address=azure_native.network.SubResourceArgs(id=public_ip.id),
        )],
        frontend_ports=[
            azure_native.network.ApplicationGatewayFrontendPortArgs(name="port_80", port=80),
            azure_native.network.ApplicationGatewayFrontendPortArgs(name="port_443", port=443),
        ],
        backend_address_pools=[
            azure_native.network.ApplicationGatewayBackendAddressPoolArgs(name="backendPool"),
        ],
        backend_http_settings_collection=[
            azure_native.network.ApplicationGatewayBackendHttpSettingsArgs(
                name="backendHttpSettings",
                port=app_port, protocol="Http",
                cookie_based_affinity="Disabled",
                request_timeout=1800,
                probe=azure_native.network.SubResourceArgs(id=f"{ag_prefix}/probes/healthProbe"),
            )
        ],
        probes=[azure_native.network.ApplicationGatewayProbeArgs(
            name="healthProbe",
            protocol="Http", host="127.0.0.1",
            path="/health",
            interval=30, timeout=10, unhealthy_threshold=3,
        )],
        http_listeners=http_listeners,
        request_routing_rules=routing_rules,
        ssl_certificates=ssl_certs,
        redirect_configurations=redirect_configurations,
        tags=tags,
        opts=pulumi.ResourceOptions(depends_on=[public_ip, kv_access, vm_nsg]),
    )

# ─────────────────────────────────────────────────────────────────────────────
# Startup script — install Docker, clone repo, write .env, start Superset
# ─────────────────────────────────────────────────────────────────────────────
_compose_files = (
    "-f docker-compose.yml -f docker-compose.local-redis.yml"
    if redis_mode == "local"
    else "-f docker-compose.yml"
)

def _build_startup(redis_host: str) -> str:
    return f"""#!/bin/bash
set -e
exec > >(tee /var/log/superset-init.log | logger -t superset-init) 2>&1

# startup_code_version={startup_code_version}

apt-get update -y
apt-get install -y apt-transport-https ca-certificates curl gnupg software-properties-common git jq unzip

# Azure CLI
curl -sL https://aka.ms/InstallAzureCLIDeb | bash

# Docker + Compose v2
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

# Fetch secrets from Key Vault via Managed Identity
az login --identity
SECRET_JSON=$(az keyvault secret show --vault-name {key_vault_name} --name {secret_id} --query value -o tsv)

GIT_USER=$(echo "$SECRET_JSON" | jq -r '.git_deploy_username // "x-token-auth"')
GIT_TOKEN=$(echo "$SECRET_JSON" | jq -r '.git_deploy_token')

# Clone repo
REPO_URL="{git_repo_url}"
REPO_PATH=$(echo "$REPO_URL" | sed 's,https://,,')
git clone "https://$GIT_USER:$GIT_TOKEN@$REPO_PATH" /home/ubuntu/superset
git -C /home/ubuntu/superset checkout {git_branch}
git -C /home/ubuntu/superset rev-parse HEAD > /home/ubuntu/superset/.schema-version

# Write .env from Key Vault secret key=value pairs
echo "$SECRET_JSON" | jq -r 'to_entries[] | .key + "=" + (.value | tostring)' > /home/ubuntu/superset/.env
chmod 600 /home/ubuntu/superset/.env

# Inject Pulumi-managed values (override any KV defaults)
echo "SQLALCHEMY_DATABASE_URI={sqlalchemy_database_uri}" >> /home/ubuntu/superset/.env
echo "REDIS_HOST={redis_host}" >> /home/ubuntu/superset/.env
echo "REDIS_PORT={_redis_port}" >> /home/ubuntu/superset/.env

# Fix shell script permissions
chmod +x /home/ubuntu/superset/docker/*.sh

# Run DB init (migrations + admin user) then start app
cd /home/ubuntu/superset
docker-compose {_compose_files} run --rm superset-init
docker-compose {_compose_files} up -d superset superset-worker superset-worker-2 superset-worker-beat
"""

_startup_output = _redis_host_output.apply(_build_startup)
custom_data = _startup_output.apply(lambda s: base64.b64encode(s.encode()).decode())

# ─────────────────────────────────────────────────────────────────────────────
# VMSS
# ─────────────────────────────────────────────────────────────────────────────
vmss = azure_native.compute.VirtualMachineScaleSet(
    f"{project_name}-vmss{suffix}",
    resource_group_name=resource_group,
    vm_scale_set_name=f"{project_name}-vmss{suffix}",
    location=azure_location,
    sku=azure_native.compute.SkuArgs(name=vm_size, capacity=1, tier="Standard"),
    identity=identity.id.apply(lambda iid: azure_native.compute.VirtualMachineScaleSetIdentityArgs(
        type="UserAssigned",
        user_assigned_identities={iid: {}},
    )),
    upgrade_policy=azure_native.compute.UpgradePolicyArgs(mode="Manual"),
    virtual_machine_profile=azure_native.compute.VirtualMachineScaleSetVMProfileArgs(
        os_profile=azure_native.compute.VirtualMachineScaleSetOSProfileArgs(
            computer_name_prefix=f"{project_name[:9]}{suffix[:3] if suffix else ''}",
            admin_username="ubuntu",
            linux_configuration=azure_native.compute.LinuxConfigurationArgs(
                disable_password_authentication=True,
                ssh=azure_native.compute.SshConfigurationArgs(
                    public_keys=[azure_native.compute.SshPublicKeyArgs(
                        path="/home/ubuntu/.ssh/authorized_keys",
                        key_data=ssh_public_key,
                    )],
                ),
            ),
            custom_data=custom_data,
        ),
        storage_profile=azure_native.compute.VirtualMachineScaleSetStorageProfileArgs(
            image_reference=azure_native.compute.ImageReferenceArgs(
                publisher="Canonical",
                offer="0001-com-ubuntu-server-jammy",
                sku="22_04-lts-gen2",
                version="latest",
            ),
            os_disk=azure_native.compute.VirtualMachineScaleSetOSDiskArgs(
                create_option="FromImage",
                disk_size_gb=volume_size,
                managed_disk=azure_native.compute.VirtualMachineScaleSetManagedDiskParametersArgs(
                    storage_account_type="Premium_LRS",
                ),
            ),
        ),
        network_profile=azure_native.compute.VirtualMachineScaleSetNetworkProfileArgs(
            network_interface_configurations=[
                azure_native.compute.VirtualMachineScaleSetNetworkConfigurationArgs(
                    name=f"{project_name}-nic{suffix}",
                    primary=True,
                    ip_configurations=[
                        azure_native.compute.VirtualMachineScaleSetIPConfigurationArgs(
                            name=f"{project_name}-ipconfig{suffix}",
                            subnet=azure_native.compute.ApiEntityReferenceArgs(id=vm_subnet_id),
                            application_gateway_backend_address_pools=[
                                azure_native.network.SubResourceArgs(
                                    id=(f"{common_app_gw_id}/backendAddressPools/superset-pool" if use_common_lb
                                        else f"{ag_prefix}/backendAddressPools/backendPool")
                                )
                            ],
                        )
                    ],
                    network_security_group=azure_native.network.SubResourceArgs(id=vm_nsg.id),
                )
            ]
        ),
    ),
    tags=tags,
    opts=pulumi.ResourceOptions(
        depends_on=([kv_access] if use_common_lb else [app_gw, kv_access]),
        replace_on_changes=["virtualMachineProfile"],
        delete_before_replace=True,
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# Azure DNS — A record → App Gateway public IP
# ─────────────────────────────────────────────────────────────────────────────
azure_native.network.RecordSet(
    f"{project_name}-dns{suffix}",
    resource_group_name=dns_zone_rg,
    zone_name=dns_zone_name,
    relative_record_set_name=dns_record_name,
    record_type="A",
    ttl=30,
    a_records=(
        [azure_native.network.ARecordArgs(ipv4_address=common_app_gw_public_ip)]
        if use_common_lb
        else public_ip.ip_address.apply(
            lambda ip: [azure_native.network.ARecordArgs(ipv4_address=ip)] if ip else []
        )
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# KG Phase 2 — register deployed services with the Deploy Management API
# ─────────────────────────────────────────────────────────────────────────────
_kg_host     = secrets.get("KG_API_HOST", "")
_kg_token    = secrets.get("KG_API_TOKEN", "")
_kg_mappings = _dwe.get("kg_mappings")

if _kg_host and _kg_token and _kg_mappings:
    import httpx as _httpx
    import warnings as _warnings

    _adapter_name  = _kg_mappings["adapter_name"]
    _kg_props_keys = _kg_mappings.get("kg_adapter_properties", {})
    _kg_outputs    = _kg_mappings.get("kg_pulumi_outputs", {})
    _kg_services   = _kg_mappings.get("services", [])

    _pulumi_export_map = {
        "url":       pulumi.Output.from_input(f"https://{dns_record_name}.{dns_zone_name}"),
        "appgw_name": app_gw.name if app_gw else pulumi.Output.from_input(""),
        "vmss_name": vmss.name,
    }
    _out_names = list(_kg_outputs.keys())
    _out_vals  = [_pulumi_export_map.get(_kg_outputs[n], pulumi.Output.from_input("")) for n in _out_names]

    def _phase2_hydrate(*resolved):
        props = dict(zip(_out_names, resolved))
        for prop, secret_key in _kg_props_keys.items():
            props[prop] = secrets.get(secret_key, "")
        _headers = {"Authorization": f"Bearer {_kg_token}"}
        _base    = _kg_host.rstrip("/")
        try:
            _httpx.patch(
                f"{_base}/adapters/{_adapter_name}/{env}",
                json={"properties": props},
                headers=_headers,
                timeout=10,
            )
        except Exception as _exc:
            _warnings.warn(f"[dwe-kg] PATCH /adapters failed: {_exc}")

        _svc_payloads = []
        for _svc in _kg_services:
            _trigger = _svc.get("trigger_secret", "")
            if _trigger and not secrets.get(_trigger):
                continue
            _svc_props = {p: secrets.get(sk, "") for p, sk in _svc.get("properties", {}).items()}
            _svc_payloads.append({"name": _svc["name"], "properties": _svc_props})

        if _svc_payloads:
            try:
                _httpx.post(
                    f"{_base}/adapters/{_adapter_name}/{env}/services",
                    json={"services": _svc_payloads},
                    headers=_headers,
                    timeout=10,
                )
            except Exception as _exc:
                _warnings.warn(f"[dwe-kg] POST /services failed: {_exc}")

    pulumi.Output.all(*_out_vals).apply(_phase2_hydrate)

# ─────────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────────
if app_gw:
    pulumi.export("appgw_name",  app_gw.name)
pulumi.export("vmss_name",   vmss.name)
pulumi.export("url",         f"https://{dns_record_name}.{dns_zone_name}")
if public_ip:
    pulumi.export("public_ip",   public_ip.ip_address)
pulumi.export("environment", env)
