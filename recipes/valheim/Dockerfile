FROM ubuntu:24.04

# Update and upgrade system
RUN apt update && apt upgrade -y

# Install required tools and add multiverse repository
RUN apt install software-properties-common -y && \
    add-apt-repository multiverse && \
    dpkg --add-architecture i386 && \
    apt update

# Preset values for license agreement keys
RUN echo steam steam/question select "I AGREE" | debconf-set-selections && \
    echo steam steam/license note '' | debconf-set-selections

# Install steamcmd without unnecessary dependencies
RUN apt install steamcmd -y --no-install-recommends

# Permanently add /usr/games to PATH globally and for Bash shell
ENV PATH="/usr/games:$PATH"
RUN echo 'export PATH="/usr/games:$PATH"' >> /root/.bashrc

# update steamcmd
RUN steamcmd +quit

WORKDIR /app
COPY InstallUpdate.sh InstallUpdate.sh
RUN chmod +x InstallUpdate.sh
RUN bash InstallUpdate.sh

EXPOSE 2456

CMD ["bash", "valheim.sh"]
