# Docker_Proxy_Pull
Download Docker images via HTTP proxy,通过http 代理的文件下载docker镜像文件

##使用方法
usage: docker_pull.py [-h] [-o OUTPUT] [--proxy PROXY] [--no-verify]
                         [--workers WORKERS]
                         image

Download Docker image with OCI layout (batch download)

positional arguments:
  image                 Image name with tag (e.g., nginx:13.6.2, hello-world)

options:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        Output directory (default: .)
  --proxy PROXY         HTTP/HTTPS proxy (e.g., http://127.0.0.1:10808)
  --no-verify           Disable SSL verification
  --workers WORKERS     Number of concurrent downloads (default: 5)
