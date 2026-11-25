# Next Box at a hosting company (Render: render.yaml next to this file). Built from the next-box folder:
#   docker build -t nextbox .
#
# The container has no printer and no home network: NEXTBOX_HOSTED=1 is its default, so everyone previews
# and prints on paper or saves the label image from their own phone. The database is PostgreSQL
# (DATABASE_URL); the data folder here only caches ticket images and is lost on every deploy, which is
# fine. Secrets come from the host's environment (render.yaml), never from a file in the image: .dockerignore
# keeps .env out. scripts/cloud/start.sh checks them and starts the server.
#
# Every step was run the same way in a fresh Python 3.12 venv before this file was written:
# pip with constraints.txt and the postgres and heic extras installs cleanly on Linux x86_64 (the heic extra,
# iPhone photos on Snap, has ready-made wheels; constraints.txt pins pillow-heif 1.8.1).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NEXTBOX_HOSTED=1

# Fonts the labels and paper pages are drawn with (nextbox/render.py): DejaVu is the main one and the
# legend's; Noto core adds Devanagari and the symbol fonts (NotoSansSymbols2) that scripts/install.sh asks
# for on the desktop. fonts-noto-cjk (Chinese, Japanese, Korean; ~100 MB) is left out on purpose: text in
# those scripts falls back to DejaVu's boxes. tzdata: so TZ=America/New_York (render.yaml) means something.
# python:3.12-slim is Debian 13 "trixie" on Docker Hub (checked 2026-11-24); all three packages exist there.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-noto-core tzdata \
 && rm -rf /var/lib/apt/lists/*

# Not root: the server runs as its own user with a writable home (the default data folder lives there;
# nextbox/config.py falls back to the temp dir if it can't).
RUN useradd --create-home --shell /usr/sbin/nologin nextbox

WORKDIR /app
COPY --chown=nextbox:nextbox . .
# constraints.txt pins every package to the versions this release was tested with (same as install.sh).
# Editable (-e): the web app's files are served from /app/web next to the package, as on the desktop.
RUN pip install -c constraints.txt -e '.[postgres,heic]'

USER nextbox
# Render tells the app its port through PORT (10000 by default); start.sh maps it to NEXTBOX_PORT.
EXPOSE 10000
CMD ["scripts/cloud/start.sh"]
