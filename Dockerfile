# syntax=docker/dockerfile:1.7
# All base images come from JFrog (CI runners cannot reach Docker Hub/PyPI/npm).
ARG REGISTRY=cvsh.jfrog.io/cvsdigital-docker
ARG NODE_IMAGE=${REGISTRY}/node:24
ARG PYTHON_BUILD_IMAGE=${REGISTRY}/python:3.13-slim
ARG RUNTIME_IMAGE=cvsh.jfrog.io/devsecops/distroless/python:3.13

# ---- web UI -------------------------------------------------------------------
FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- python deps --------------------------------------------------------------
FROM ${PYTHON_BUILD_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=pipconf,target=/etc/pip.conf \
    pip install --target /app/deps -r requirements.txt
COPY pyproject.toml ./
COPY src/ src/
RUN --mount=type=secret,id=pipconf,target=/etc/pip.conf \
    pip install --no-deps --target /app/deps .

# ---- runtime (distroless, non-root) -----------------------------------------
FROM ${RUNTIME_IMAGE}
WORKDIR /app
COPY --from=build /app/deps /app/deps
COPY catalog/ /app/catalog/
COPY config/ /app/config/
COPY ["docs/OIPReqs", "/app/docs/OIPReqs"]
COPY --from=web /web/dist /app/web/dist
ENV PYTHONPATH=/app/deps \
    PYTHONUNBUFFERED=1 \
    SRA_ROOT=/app \
    SRA_DATA_DIR=/tmp/sra \
    PORT=8080
USER 31337:31337
EXPOSE 8080
ENTRYPOINT ["python", "-m", "sra.cli"]
CMD ["serve"]
