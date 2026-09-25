FROM docker.io/library/ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# steamcmd lives in multiverse and needs i386; pre-accept the Steam licence.
RUN apt-get update \
    && apt-get install -y --no-install-recommends software-properties-common ca-certificates \
    && add-apt-repository multiverse \
    && dpkg --add-architecture i386 \
    && echo steam steam/question select "I AGREE" | debconf-set-selections \
    && echo steam steam/license note '' | debconf-set-selections \
    && apt-get update \
    && apt-get install -y --no-install-recommends steamcmd \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/games:$PATH"
RUN steamcmd +quit

# The game is baked into the image; bump `version` in recipe.toml when rebuilding for a patch.
WORKDIR /app
COPY InstallUpdate.sh /usr/local/bin/install-update
RUN chmod +x /usr/local/bin/install-update && install-update

COPY defaults/ /opt/nexus/defaults/
COPY entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

# Everything the server persists lives under /data, which nexus backs up.
VOLUME /data
EXPOSE 2456/udp 2457/udp

ENTRYPOINT ["/usr/local/bin/entrypoint"]
