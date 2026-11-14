# No "# syntax=" line: that would pull the Dockerfile frontend from Docker Hub, which CI cannot reach. The built-in
# frontend of the pipeline's Docker supports secret mounts with env= (as OIP's Dockerfile uses them).
# Built the way OIP (FSIC COG v2) is built (OIP docs: CICD_JFROG_CAP.md, CAP_DEPLOYMENT_ARCHITECTURE.md):
# - every base image and package comes from JFrog (cvsh.jfrog.io): CI runners cannot reach Docker Hub, PyPI or npm;
# - JFrog credentials are the BuildKit secrets the shared workflow provides (artifactory_user_encoded, artifactory_token),
#   mounted for one RUN each and never written to a layer;
# - the runtime is distroless Python, non-root 31337:31337, one process on :8080 (UI, API, /healthz, scheduler).
# Local builds outside CI: pass the same two secrets, e.g.
#   docker build --secret id=artifactory_user_encoded,env=ARTIFACTORY_USERNAME --secret id=artifactory_token,env=ARTIFACTORY_TOKEN .
ARG REGISTRY=cvsh.jfrog.io/cvsdigital-docker
ARG NODE_IMAGE=${REGISTRY}/node:24
ARG PYTHON_BUILD_IMAGE=${REGISTRY}/devsecops/distroless/python-3-debug
ARG RUNTIME_IMAGE=${REGISTRY}/devsecops/distroless/python-3-distroless
ARG ARTIFACTORY_HOST=cvsh.jfrog.io

# ---- web UI -------------------------------------------------------------------
FROM ${NODE_IMAGE} AS web
ARG ARTIFACTORY_HOST
WORKDIR /web
COPY web/package.json web/package-lock.json ./
# The lockfile may name the public registry or a developer's JFrog repository; the pipeline credentials read
# cvsdigital-npm, so point every resolved URL there. Integrity hashes are unchanged, so npm still verifies each package.
# The temporary .npmrc holds the token and is deleted in the same RUN.
# Without the secrets (a local docker compose build only) npm uses its default registry.
RUN --mount=type=secret,id=artifactory_user_encoded,env=ARTIFACTORY_USERNAME \
    --mount=type=secret,id=artifactory_token,env=ARTIFACTORY_TOKEN \
    if [ -n "$ARTIFACTORY_TOKEN" ]; then \
      REG="https://${ARTIFACTORY_HOST}/artifactory/api/npm/cvsdigital-npm/" \
      && sed -i -e "s#https://registry.npmjs.org/#${REG}#g" -e "s#/api/npm/cvs-caremark-npm/#/api/npm/cvsdigital-npm/#g" package-lock.json \
      && printf 'registry=%s\n//%s/artifactory/api/npm/cvsdigital-npm/:_auth=%s\nalways-auth=true\n' \
           "$REG" "$ARTIFACTORY_HOST" "$(printf '%s:%s' "$ARTIFACTORY_USERNAME" "$ARTIFACTORY_TOKEN" | base64 | tr -d '\n')" > /tmp/.npmrc; \
    else : > /tmp/.npmrc; fi \
 && npm ci --no-audit --no-fund --userconfig /tmp/.npmrc; status=$?; rm -f /tmp/.npmrc; exit $status
COPY web/ ./
# tsc -b type-checks first, so a UI type error fails the image build (as OIP's ui stage does)
RUN npm run build

# ---- python deps --------------------------------------------------------------
# The distroless debug image has the same Python as the runtime image (and a busybox shell for RUN), so compiled
# wheels (lxml, numpy, onnxruntime, psycopg) match the interpreter that loads them.
FROM ${PYTHON_BUILD_IMAGE} AS build
ARG ARTIFACTORY_HOST
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /home/nonroot/app
COPY requirements.txt pyproject.toml ./
COPY src/ src/
# rapidocr-onnxruntime asks for opencv-python (needs libGL, absent here); its real dependencies are in requirements.txt
# with the headless OpenCV, so it is installed without its own. The package itself goes in with --no-deps as well.
RUN --mount=type=secret,id=artifactory_user_encoded,env=ARTIFACTORY_USERNAME \
    --mount=type=secret,id=artifactory_token,env=ARTIFACTORY_TOKEN \
    INDEX="https://${ARTIFACTORY_USERNAME}:${ARTIFACTORY_TOKEN}@${ARTIFACTORY_HOST}/artifactory/api/pypi/cvsdigital-pypi/simple" \
 && if [ -z "$ARTIFACTORY_TOKEN" ]; then INDEX=https://pypi.org/simple; fi \
 && python -m pip install --index-url "$INDEX" --target /home/nonroot/app/site-packages -r requirements.txt \
 && python -m pip install --index-url "$INDEX" --no-deps --target /home/nonroot/app/site-packages rapidocr-onnxruntime==1.4.4 \
 && python -m pip install --index-url "$INDEX" --no-deps --target /home/nonroot/app/site-packages .

# ---- runtime (distroless, non-root) -----------------------------------------
FROM ${RUNTIME_IMAGE}
WORKDIR /app
COPY --from=build /home/nonroot/app/site-packages /app/site-packages
COPY catalog/ /app/catalog/
COPY config/ /app/config/
# The OIPReqs standards, playbooks and templates (the same library OIP's Reference library loads). No ServiceNow or
# Splunk exports are baked in: evidence arrives by upload or the connector and is kept in Postgres.
COPY ["docs/OIPReqs", "/app/docs/OIPReqs"]
COPY --from=web /web/dist /app/web/dist
ENV PYTHONPATH=/app/site-packages \
    PYTHONUNBUFFERED=1 \
    SRA_ROOT=/app \
    SRA_DATA_DIR=/tmp/sra \
    PORT=8080
USER 31337:31337
EXPOSE 8080
# exec form: distroless has no shell
ENTRYPOINT ["python", "-m", "sra.cli"]
CMD ["serve"]
