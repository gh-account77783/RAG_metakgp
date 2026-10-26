# syntax=docker/dockerfile:1.7
# Named contexts: bundle = approved Linux bundle; source = public build inputs only.
ARG PYTHON_IMAGE=python:3.13.17-slim-bookworm
ARG KEYCLOAK_IMAGE=quay.io/keycloak/keycloak:26.7.5
ARG JDK_IMAGE=eclipse-temurin:21.0.12.1_1-jdk-jammy
FROM ${PYTHON_IMAGE} AS python-base
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libstdc++6 libfontconfig1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1

FROM python-base AS installed
ARG BUNDLE_MANIFEST_SHA256
COPY --from=source tools/bundle_installer.py /bootstrap/install_bundle.py
RUN --network=none --mount=from=bundle,target=/bundle,ro \
    python -I -S -B /bootstrap/install_bundle.py /bundle \
      --manifest-sha256 "${BUNDLE_MANIFEST_SHA256}" --environment /opt/graphmind

FROM python-base AS application
COPY --from=installed /opt/graphmind /opt/graphmind
COPY runtime.py bootstrap_identity.py healthcheck.py /opt/graphmind-container/
RUN groupadd --gid 10001 graphmind && useradd --uid 10001 --gid 10001 --no-create-home graphmind \
    && mkdir -p /var/lib/graphmind /var/lib/chroma /imports /models /etc/graphmind \
    && chown 10001:10001 /var/lib/graphmind /var/lib/chroma
ENV HOME=/var/lib/graphmind/home HF_HOME=/var/lib/graphmind/cache/huggingface \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    TOKENIZERS_PARALLELISM=false ANONYMIZED_TELEMETRY=false
USER 10001:10001
WORKDIR /var/lib/graphmind
ENTRYPOINT ["/opt/graphmind/bin/python", "-I", "-B", "/opt/graphmind-container/runtime.py"]
CMD ["reader"]

FROM ${JDK_IMAGE} AS jdk
FROM ${KEYCLOAK_IMAGE} AS keycloak-base
FROM python-base AS provider
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
COPY --from=keycloak-base /opt/keycloak/lib/lib/main /keycloak-libs
COPY --from=source tools/build_keycloak_recovery.py /source/tools/build_keycloak_recovery.py
COPY --from=source deploy/keycloak/recovery-src /source/deploy/keycloak/recovery-src
RUN --network=none python -I -S -B /source/tools/build_keycloak_recovery.py \
    --jdk-bin /opt/java/openjdk/bin --classpath-dir /keycloak-libs --output /provider

FROM keycloak-base AS keycloak-built
COPY --from=provider --chown=1000:0 /provider/graphmind-keycloak-recovery-26.7.5.jar /opt/keycloak/providers/
COPY --from=source --chown=1000:0 deploy/keycloak/themes/graphmind /opt/keycloak/themes/graphmind
RUN --network=none touch -m --date=@1767225600 /opt/keycloak/providers/graphmind-keycloak-recovery-26.7.5.jar \
    && /opt/keycloak/bin/kc.sh build --db=postgres --health-enabled=true

FROM keycloak-base AS keycloak
COPY --from=keycloak-built --chown=1000:0 /opt/keycloak /opt/keycloak
COPY --chmod=0555 keycloak-start.sh /opt/keycloak/bin/graphmind-start.sh
ENTRYPOINT ["/bin/bash", "/opt/keycloak/bin/graphmind-start.sh"]
