# Hermes Agent as a Junto MCP client, isolated in a container
# (design:hermes-frontdesk-pilot-v0). The official installer edits ~/.bashrc and
# installs its own Python/Node, so keep it off a host that runs other agents.
#
#   curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o install.sh   # read it first
#   docker build --network host -t hermes-junto .
#   docker volume create hermes-junto
#   docker run --rm --network host -v hermes-junto:/home/hermes/.hermes hermes-junto \
#     bash -lc 'sh /home/hermes/configure.sh'                                     # once
#   docker run --rm -it --network host -v hermes-junto:/home/hermes/.hermes hermes-junto \
#     bash -lc 'PATH=$HOME/.local/bin:$PATH hermes -z "..."'
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates libatomic1 xz-utils procps \
 && rm -rf /var/lib/apt/lists/* && useradd -m hermes
USER hermes
WORKDIR /home/hermes
COPY --chown=hermes install.sh configure.sh /home/hermes/
RUN bash install.sh --non-interactive
