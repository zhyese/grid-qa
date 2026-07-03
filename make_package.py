"""打包完整部署包：源码 + 全部数据 -> tar.gz"""
import tarfile
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

INCLUDE = [
    "docker-compose.deploy.yml",
    "docker-compose.yml",
    "prometheus.yml",
    ".env.deploy",
    ".env.example",
    "backend",
    "frontend",
    "grafana",
    "kb_seed",
    "data",
]

EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", "venv", ".claude"}
EXCLUDE_EXT = {".pyc", ".jpg"}
SKIP_NAMES = {"mysql.sock", "mysql.sock.lock"}


def add_to_tar(tar, path, base, stats):
    """Add a file or directory tree to tar."""
    if os.path.isfile(path):
        f = os.path.basename(path)
        if f in SKIP_NAMES or f.endswith(".sock"):
            return
        for ext in EXCLUDE_EXT:
            if path.endswith(ext):
                return
        arcname = os.path.relpath(path, base).replace("\\", "/")
        tar.add(path, arcname=arcname)
        stats["count"] += 1
        return

    # Directory
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f in SKIP_NAMES or f.endswith(".sock"):
                continue
            fp = os.path.join(root, f)
            skip = False
            for ext in EXCLUDE_EXT:
                if fp.endswith(ext):
                    skip = True
                    break
            if skip:
                continue
            arcname = os.path.relpath(fp, base).replace("\\", "/")
            tar.add(fp, arcname=arcname)
            stats["count"] += 1
            if stats["count"] % 2000 == 0:
                print("    {} files...".format(stats["count"]))


def main():
    name = "grid-qa-deploy-{}.tar.gz".format(time.strftime("%Y%m%d-%H%M%S"))
    print("Packing to {}...".format(name))

    stats = {"count": 0}
    with tarfile.open(name, "w:gz") as tar:
        for item in INCLUDE:
            full = os.path.join(BASE, item)
            if not os.path.exists(full):
                print("  SKIP (not found): {}".format(item))
                continue
            print("  Adding: {} ...".format(item))
            add_to_tar(tar, full, BASE, stats)

    size_mb = os.path.getsize(name) / 1024 / 1024
    print("")
    print("=" * 50)
    print(" Package: {}".format(name))
    print(" Files:  {}".format(stats["count"]))
    print(" Size:   {:.0f} MB".format(size_mb))
    print("=" * 50)
    print("")
    print("Deploy on remote:")
    print("  1. scp {} user@remote:/opt/".format(name))
    print("  2. ssh user@remote")
    print("  3. cd /opt && tar xzf {}".format(name))
    print("  4. cp .env.deploy .env   # edit .env with real API Keys")
    print("  5. docker compose -f docker-compose.deploy.yml up -d --build")
    print("  6. curl http://localhost:8001/health")


if __name__ == "__main__":
    main()
