import docker

# client = docker.from_env()
client = docker.DockerClient(base_url="unix://var/run/docker.sock")
