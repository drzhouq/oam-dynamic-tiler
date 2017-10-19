# This is adopted from OAM tiler
FROM satelytics/oambase
MAINTAINER John Zhou <jzhou@satelytics.com>

RUN \
  pip install -U gevent gunicorn && \
  rm -rf /root/.cache

# override this accordingly; should be 2-4x $(nproc)
ENV WEB_CONCURRENCY 4
EXPOSE 8000
# we should add app.py and tiler.py here

USER nobody
COPY static /opt/oam-dynamic-tiler/static
COPY templates /opt/oam-dynamic-tiler/templates

COPY app.py /opt/oam-dynamic-tiler/app.py
COPY tiler.py /opt/oam-dynamic-tiler/tiler.py

#ENTRYPOINT ["gunicorn", "-k", "gevent", "-b", "0.0.0.0", "--access-logfile", "-", "--error-logfile", "-", "--log-level", "debug", "--reload","app:app"]
ENTRYPOINT ["gunicorn", "-k", "gevent", "-b", "0.0.0.0", "--access-logfile", "-", "app:app"]
