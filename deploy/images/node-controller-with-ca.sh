#!/bin/sh
set -eu

# The secret is visible only during this BuildKit RUN and is never copied.
if [ -f /run/secrets/proxy_ca ]; then
  SSL_CERT_FILE=/run/secrets/proxy_ca
  export SSL_CERT_FILE
fi

exec "$@"
