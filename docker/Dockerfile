FROM weblate/dev:2026.9.0@sha256:2c93f762c32c569d357e254f8d215d489153cddab9f4155d936f34ffdc590134 AS build

ENV WEBLATE_EXTRAS=all,MySQL,zxcvbn,saml

SHELL ["/bin/bash", "-o", "pipefail", "-x", "-c"]

COPY --link docker/requirements.txt docker/patches /app/src/

# Install boost-weblate source
COPY . /app/boost-weblate/
# hadolint ignore=DL3008,DL3013,SC2046,DL3003,SC1091
RUN \
  --mount=type=tmpfs,target=/tmp \
  --mount=type=cache,target=/.uv-cache,sharing=locked \
  export UV_CACHE_DIR=/.uv-cache UV_LINK_MODE=copy \
  && uv venv --python "python${PYVERSION}" /app/venv \
  && . /app/venv/bin/activate \
  && uv --version \
  && python --version \
  && uv pip install \
      --compile-bytecode \
      --no-binary xmlsec \
      --no-binary lxml \
      -r /app/src/requirements.txt \
      "/app/boost-weblate[${WEBLATE_EXTRAS}]" \
  && rm -rf /app/venv/lib/python*/site-packages/slapdtest \
  && uv cache prune --ci \
  && du -sh "$UV_CACHE_DIR" \
  && /app/venv/bin/python -c 'from phply.phpparse import make_parser; make_parser()' \
  && ln -s /app/venv/share/weblate/examples/ /app/

# Apply hotfixes on Weblate
RUN find /app/src -name '*.patch' -print0 | sort -z | \
  xargs -n1 -0 -r patch -p1 -d "/app/venv/lib/python${PYVERSION}/site-packages/" -i


FROM weblate/base:2026.9.0@sha256:2d80c7fa7d54006a3010d8e93e65075df354c9fef75761203ce4fa5e5d29b03b AS final

ENV BOOST_WEBLATE_VERSION=1.0.0

LABEL name="boost-weblate"
LABEL version=$BOOST_WEBLATE_VERSION

# Increased start period for migrations run
HEALTHCHECK --interval=30s --timeout=3s --start-period=5m CMD /app/bin/health_check

# Use Docker specific settings
ENV DJANGO_SETTINGS_MODULE=weblate.settings_docker

# Copy built environment
COPY --from=build /app /app

# Configuration for Weblate, nginx and supervisor
COPY --link docker/etc /etc/

# Customize Python:
# - Search path for custom modules
RUN \
    echo "/app/data/python" > "/app/venv/lib/python${PYVERSION}/site-packages/weblate-docker.pth" && \
    mkdir -p /app/data/python/customize && \
    touch /app/data/python/customize/__init__.py && \
    touch /app/data/python/customize/models.py && \
    chown -R weblate:weblate /app/data/python

# Fix permissions and adjust files to be able to edit them as user on start
RUN rm -f /etc/localtime /etc/timezone \
  && ln -s /tmp/localtime /etc/localtime \
  && chgrp -R 0 /var/log/nginx/ /var/lib/nginx /app/data /app/cache /run /home/weblate /etc/supervisor/conf.d \
  && chmod -R 770 /var/log/nginx/ /var/lib/nginx /app/data /app/cache /run /home /home/weblate /etc/supervisor/conf.d \
  && rm -f /etc/nginx/sites-available/default \
  && ln -s /tmp/nginx/weblate-site.conf /etc/nginx/sites-available/default \
  && rm -f /var/log/nginx/access.log /var/log/nginx/error.log \
  && ln -sf /dev/stdout /var/log/nginx/access.log \
  && ln -sf /dev/stderr /var/log/nginx/error.log \
  && rm -rf /run/* \
  && chmod 664 /etc/passwd \
  && sed -i '/pam_rootok.so/a auth requisite pam_deny.so' /etc/pam.d/su

# Install po4a v0.74 from source (required by weblate/formats/asciidoc.py)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libyaml-tiny-perl \
        build-essential \
        libmodule-build-perl \
        gettext \
        libxml2-utils \
        docbook-xsl \
        xsltproc \
    && cd /tmp \
    && curl -fsSL -o po4a-0.74.tar.gz \
        https://github.com/mquinson/po4a/releases/download/v0.74/po4a-0.74.tar.gz \
    && echo "25fc323f2ba37bbd48c3af0ebf49952644b0e468261f98633e91219a838fe7c2  po4a-0.74.tar.gz" \
        | sha256sum -c - \
    && tar xzf po4a-0.74.tar.gz \
    && cd po4a-0.74 \
    && perl Build.PL \
    && ./Build build \
    && ./Build install \
    && cd /tmp \
    && rm -rf po4a-0.74.tar.gz po4a-0.74 \
    && apt-get purge -y build-essential libmodule-build-perl xsltproc docbook-xsl libxml2-utils curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Entrypoint
COPY --link --chmod=0755 docker/start docker/health_check /app/bin/

EXPOSE 8080
VOLUME /app/data
VOLUME /app/cache

# Numerical value is needed for OpenShift S2I
USER 1000

ENTRYPOINT ["/app/bin/start"]
CMD ["runserver"]
