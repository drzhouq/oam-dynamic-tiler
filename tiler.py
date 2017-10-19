# coding=utf-8
from __future__ import division

from functools import partial
import logging
from multiprocessing.dummy import Pool
from StringIO import StringIO
import os

from cachetools.func import lru_cache, ttl_cache
import mercantile
import numpy as np
from PIL import Image
import rasterio
import requests
from rio_color import operations


S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "")
pool = Pool(100)


# normalize prefix
if S3_PREFIX == "/":
    S3_PREFIX = ""

if not S3_PREFIX.endswith("/"):
    S3_PREFIX += "/"

if S3_PREFIX.startswith("/"):
    S3_PREFIX = S3_PREFIX[1:]


def get_id(id, image_id=None, scene_idx=0):
    if image_id:
        return '{}/{}/{}'.format(id, scene_idx, image_id)

    return id


@ttl_cache(ttl=300)
def get_metadata(id, image_id=None, scene_idx=0, **kwargs):
    if image_id:
        rsp = requests.get('http://{}.s3.amazonaws.com/{}{}/{}/{}.json'.format(S3_BUCKET, S3_PREFIX, id, scene_idx, image_id))
    else:
        rsp = requests.get('http://{}.s3.amazonaws.com/{}{}/{}/scene.json'.format(S3_BUCKET, S3_PREFIX, id, scene_idx))

    if not rsp.ok:
        raise InvalidTileRequest('Could not load {}'.format(rsp.url))

    return rsp.json()


@lru_cache(maxsize=1024)
def get_source(path):
    return rasterio.open(path)


def read_window(window, src_url, mask_url=None, scale=1):
    tile_size = 256 * scale

    with rasterio.Env(CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.vrt,.tif,.ovr,.msk'):
        src = get_source(src_url)

        # TODO read the data and the mask in parallel
        if mask_url:
            data = src.read(out_shape=(3, tile_size, tile_size), window=window)
            mask = get_source(mask_url)
            mask_data = mask.read(out_shape=(1, tile_size, tile_size), window=window)

            return np.concatenate((data, mask_data))
        else:
            if src.count == 4:
                # alpha channel present
                return src.read(out_shape=(4, tile_size, tile_size), window=window)
            else:
                # no alpha channel, create one
                # TODO use src.bounds as an implicit mask
                data = src.read(out_shape=(3, tile_size, tile_size), window=window)
                alpha = np.full((1, tile_size, tile_size), np.iinfo(data.dtype).max, data.dtype)

                return np.concatenate((data, alpha))


def make_window(src_tile_zoom, tile):
    dz = src_tile_zoom - tile.z
    x = 2**dz * tile.x
    y = 2**dz * tile.y
    mx = 2**dz * (tile.x + 1)
    my = 2**dz * (tile.y + 1)
    dx = mx - x
    dy = my - y
    top = (2**src_tile_zoom * 256) - 1

    # y, x (rows, columns)
    # window is measured in pixels at src_tile_zoom
    return ((top - (top - (256 * y)), top - (top - ((256 * y) + int(256 * dy)))),
            (256 * x, (256 * x) + int(256 * dx)))


def read_masked_window(source, tile, scale=1):
    return read_window(
        make_window(source['meta']['approximateZoom'], tile),
        source['meta'].get('source'),
        source['meta'].get('mask'),
        scale=scale
    )


def intersects(tile):
    t = mercantile.bounds(*tile)

    def _intersects(src):
        (left, bottom, right, top) = src['bounds']
        return not(left >= t.east or right <= t.west or top <= t.south or bottom >= t.north)

    return _intersects


def render_tile(meta, tile, scale=1):
    src_url = meta['meta'].get('source')
    if src_url:
        return read_window(
            make_window(meta['meta']['approximateZoom'], tile),
            src_url,
            meta['meta'].get('mask'),
            scale=scale
        )
    else:
        # optimize by filtering sources to only include those that apply to this tile
        sources = filter(intersects(tile), meta['meta'].get('sources', []))

        if len(sources) == 1:
            return read_window(
                make_window(sources[0]['meta']['approximateZoom'], tile),
                sources[0]['meta']['source'],
                sources[0]['meta'].get('mask'),
                scale=scale
            )

        data = np.zeros(shape=(4, 256 * scale, 256 * scale)).astype(np.uint8)

        # read windows in parallel and alpha composite
        for d in pool.map(partial(read_masked_window, tile=tile, scale=scale), sources):
            mask = d[3] > 0
            mask = mask[np.newaxis,:]
            data = np.where(mask, d, data)

        return data


class InvalidTileRequest(Exception):
    status_code = 404

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv['message'] = self.message
        return rv


def get_bounds(id, **kwargs):
    return get_metadata(id, **kwargs)['bounds']


def read_tile(id, tile, scale=1, **kwargs):
    meta = get_metadata(id, **kwargs)
    maxzoom = int(meta['maxzoom'])
    minzoom = int(meta['minzoom'])

    if not minzoom <= tile.z <= maxzoom:
        raise InvalidTileRequest('Invalid zoom: {} outside [{}, {}]'.format(tile.z, minzoom, maxzoom))

    sw = mercantile.tile(*meta['bounds'][0:2], zoom=tile.z)
    ne = mercantile.tile(*meta['bounds'][2:4], zoom=tile.z)

    if not sw.x <= tile.x <= ne.x:
        raise InvalidTileRequest('Invalid x coordinate: {} outside [{}, {}]'.format(tile.x, sw.x, ne.x))

    if not ne.y <= tile.y <= sw.y:
        raise InvalidTileRequest('Invalid y coordinate: {} outside [{}, {}]'.format(tile.y, sw.y, ne.y))

    data = render_tile(meta, tile, scale=scale)

    # 8-bit per pixel
    target_dtype = np.uint8

    # default values from rio color atmo
    ops = meta['meta'].get('operations')
    if ops:
        # scale to (0..1)
        floats = (data * 1.0 / np.iinfo(data.dtype).max).astype(np.float32)

        for func in operations.parse_operations(ops):
            floats = func(floats)

        # scale back to uint8
        data = (floats * np.iinfo(target_dtype).max).astype(target_dtype)

    if data.dtype != target_dtype:
        # rescale
        try:
            data = (data * (np.iinfo(target_dtype).max / np.iinfo(data.dtype).max)).astype(target_dtype)
        except:
            raise Exception('Not enough information to rescale; source is "{}""'.format(data.dtype))

    imgarr = np.ma.transpose(data, [1, 2, 0])

    out = StringIO()
    im = Image.fromarray(imgarr, 'RGBA')
    im.save(out, 'png')

    return out.getvalue()
