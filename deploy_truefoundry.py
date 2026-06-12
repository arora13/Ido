from __future__ import annotations

import os

from truefoundry.deploy import (
    Build,
    DockerFileBuild,
    LocalSource,
    NodeSelector,
    Port,
    Resources,
    Service,
)


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


service = Service(
    name=os.getenv("TFY_SERVICE_NAME", "cad-agent-api"),
    image=Build(
        build_source=LocalSource(project_root_path="./", local_build=True),
        build_spec=DockerFileBuild(
            dockerfile_path="./Dockerfile",
            build_context_path="./",
        ),
    ),
    resources=Resources(
        cpu_request=0.25,
        cpu_limit=0.5,
        memory_request=512,
        memory_limit=1024,
        ephemeral_storage_request=256,
        ephemeral_storage_limit=512,
        node=NodeSelector(capacity_type="spot_fallback_on_demand"),
    ),
    env={
        "CAD_AGENT_PROVIDER": os.getenv("CAD_AGENT_PROVIDER", "openai"),
        "CAD_AGENT_DEMO_MODE": os.getenv("CAD_AGENT_DEMO_MODE", "false"),
        "OPENAI_API_KEY": required("OPENAI_API_KEY"),
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "gpt-5.5"),
    },
    ports=[
        Port(
            port=8000,
            protocol="TCP",
            expose=True,
            app_protocol="http",
            host=required("TFY_SERVICE_HOST"),
        )
    ],
    replicas=1.0,
)

service.deploy(workspace_fqn=required("TFY_WORKSPACE_FQN"), wait=True)

