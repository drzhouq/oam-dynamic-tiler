#!/usr/bin/env python
# coding=utf-8

from __future__ import print_function

import json
import math
import os
import re
import sys

import click
import rasterio
from rasterio.warp import transform_bounds

from get_zoom import get_zoom, get_zoom_offset


@click.command()
@click.option("--include-mask", is_flag=True, help="Include a mask URL")
@click.argument("prefix")
def get_metadata(include_mask, prefix):
    scene = "{}.tif".format(prefix)
    scene_vrt = "{}_warped.vrt".format(prefix)
    mask_vrt = "{}_warped_mask.vrt".format(prefix)
    footprint = "{}_footprint.json".format(prefix)

    with rasterio.Env():
        input = re.sub("s3://([^/]+)/", "http://\\1.s3.amazonaws.com/", scene)
        try:
            with rasterio.open(input) as src:
                bounds = transform_bounds(src.crs, {'init': 'epsg:4326'}, *src.bounds)
                approximate_zoom = get_zoom(scene)
                maxzoom = approximate_zoom + 3
                minzoom = max(approximate_zoom - get_zoom_offset(src.width, src.height, approximate_zoom), 0)
                source = re.sub("s3://([^/]+)/", "http://\\1.s3.amazonaws.com/", scene_vrt)
                mask = re.sub("s3://([^/]+)/", "http://\\1.s3.amazonaws.com/", mask_vrt)
                footprint = re.sub("s3://([^/]+)/", "http://\\1.s3.amazonaws.com/", footprint)

                meta = {
                  "bounds": bounds,
                  "center": [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2, (minzoom + approximate_zoom) / 2],
                  "maxzoom": maxzoom,
                  "meta": {
                    "approximateZoom": approximate_zoom,
                    "footprint": footprint,
                    "height": src.height,
                    "source": source,
                    "width": src.width,
                  },
                  "minzoom": minzoom,
                  # TODO provide a name
                  "name": prefix,
                  "tilejson": "2.1.0"
                }

                if include_mask:
                    meta['meta']['mask'] = mask

                print(json.dumps(meta))
        except (IOError, rasterio._err.CPLE_HttpResponseError) as e:
            print("Unable to open '{}': {}".format(input, e), file=sys.stderr)
            exit(1)


if __name__ == "__main__":
    get_metadata()
