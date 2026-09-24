FROM catthehacker/ubuntu:act-22.04
COPY --from=registry.k8s.io/kubectl:v1.35.5 /bin/kubectl /usr/local/bin/kubectl
COPY --from=aquasec/trivy:0.70.0 /usr/local/bin/trivy /usr/local/bin/trivy
COPY --from=ghcr.io/sigstore/cosign/cosign:v3.1.3 /ko-app/cosign /usr/local/bin/cosign
RUN python3 -m pip install -q pyyaml==6.0.3 \
 && kubectl version --client >/dev/null && trivy --version >/dev/null && cosign version >/dev/null
