#!/usr/bin/env bash
#for some reason, if no extra args (metadata) is provided, the script will error out.
#the orginal version was kept as comment
#new version removed any reference to args in total 3 places
# capture arguments (we'll pass them to oin-meta-generator)
args=("${@:1:$[$#-2]}")
shift $[$#-2]

input=$1
output=$2

THUMBNAIL_SIZE=${THUMBNAIL_SIZE:-300} # target size in KB
#TILER_BASE_URL=${TILER_BASE_URL:-http://tiles.openaerialmap.org}
TILER_BASE_URL=${TILER_BASE_URL:-http://tiles.satelytics.io}

# Although TMPDIR is usually set by the OS, it doesn't seem to be getting passed through
# Docker and Node's `spawn()`, so no harm done in just defaulting to `/tmp` in such cases.
# There may be clues here as the underlying issue: https://github.com/npm/npm/issues/4531
TMPDIR={TMPDIR:-/tmp}

set -euo pipefail

to_clean=()

function cleanup() {
  for f in ${to_clean[@]}; do
    rm -f "${f}"
  done
}

function cleanup_on_failure() {
  s3_outputs=(${output}.tif ${output}.tif.msk ${output}_footprint.json ${output}.vrt ${output}_thumb.png ${output}_warped.vrt ${output}_warped_mask.vrt ${output}.json)

  set +e
  for x in ${s3_outputs[@]}; do
    aws s3 rm $x 2> /dev/null
  done
  set -e

  cleanup
}

if [[ -z "$input" || -z "$output" ]]; then
  # input is an HTTP-accessible GDAL-readable image
  # output is an S3 URI w/o extensions
  # e.g.:
  #   bin/process.sh \
  #   http://hotosm-oam.s3.amazonaws.com/uploads/2016-12-29/58655b07f91c99bd00e9c7ab/scene/0/scene-0-image-0-transparent_image_part2_mosaic_rgb.tif \
  #   s3://oam-dynamic-tiler-tmp/sources/58655b07f91c99bd00e9c7ab/0/58655b07f91c99bd00e9c7a6
  >&2 echo "usage: $(basename $0) <input> <output>"
  exit 1
fi

trap cleanup EXIT
trap cleanup_on_failure INT
trap cleanup_on_failure ERR

__dirname=$(cd $(dirname "$0"); pwd -P)
PATH=$__dirname:${__dirname}/../node_modules/.bin:$PATH
base=$(mktemp)
source=$base
to_clean+=($source)
intermediate=${base}-intermediate.tif
to_clean+=($intermediate)
http_output=$(sed 's|s3://\([^/]*\)/|http://\1.s3.amazonaws.com/|' <<< $output)
tiler_url=$(sed "s|s3://[^/]*|${TILER_BASE_URL}|" <<< $output)

filename=$(basename $input)
ext="${filename##*.}"

# 0. download source (if appropriate)
if [[ ( "$input" =~ ^s3:// || "$input" =~ s3\.amazonaws\.com ) && "$ext" =~ ^tiff? ]]; then
  source=$input
else
  >&2 echo "Downloading $input..."
  curl -sfL $input -o $source
fi

# 1. transcode + generate overviews
>&2 echo "Transcoding..."
transcode.sh $source $intermediate
rm -f $source

# 2. generate metadata
>&2 echo "Generating OIN metadata..."
metadata=$(oin-meta-generator -u "${http_output}.tif" -m "thumbnail=${http_output}_thumb.png" -m "tms=${tiler_url}/{z}/{x}/{y}.png" -m "wmts=${tiler_url}/wmts"  $intermediate)
#metadata=$(oin-meta-generator -u "${http_output}.tif" -m "thumbnail=${http_output}_thumb.png" -m "tms=${tiler_url}/{z}/{x}/{y}.png" -m "wmts=${tiler_url}/wmts" "${args[@]}" $intermediate)

# 2. upload TIF
>&2 echo "Uploading..."
aws s3 cp $intermediate ${output}.tif --acl public-read

if [ -f ${intermediate}.msk ]; then
  mask=1

  # 3. upload mask
  >&2 echo "Uploading mask..."
  aws s3 cp ${intermediate}.msk ${output}.tif.msk --acl public-read

  # 4. create RGBA VRT (for use in QGIS, etc.)
  >&2 echo "Generating RGBA VRT..."
  vrt=${base}.vrt
  to_clean+=($vrt)
  http_output=${output/s3:\/\//http:\/\/s3.amazonaws.com\/}
  gdal_translate \
    -b 1 \
    -b 2 \
    -b 3 \
    -b mask \
    -of VRT \
    /vsicurl/${http_output}.tif $vrt

  cat $vrt | \
    perl -pe 's|(band="4"\>)|$1\n    <ColorInterp>Alpha</ColorInterp>|' | \
    perl -pe "s|/vsicurl/${http_output}|$(basename $output)|" | \
    perl -pe 's|(relativeToVRT=)"0"|$1"1"|' | \
    aws s3 cp - ${output}.vrt --acl public-read

  # 5. create footprint
  >&2 echo "Generating footprint..."
  rio shapes --mask --as-mask --sampling 100 --precision 6 $intermediate | \
    aws s3 cp - ${output}_footprint.json --acl public-read
else
  mask=0

  # 3. create RGB VRT (for parity)
  >&2 echo "Generating RGB VRT..."
  vrt=${base}.vrt
  to_clean+=($vrt)
  gdal_translate \
    -of VRT \
    /vsicurl/${http_output}.tif $vrt

  cat $vrt | \
    perl -pe "s|/vsicurl/${http_output}|$(basename $output)|" | \
    perl -pe 's|(relativeToVRT=)"0"|$1"1"|' | \
    aws s3 cp - ${output}.vrt --acl public-read

  # 4. create footprint (bounds of image)
  >&2 echo "Generating footprint..."
  rio bounds $intermediate | \
    aws s3 cp - ${output}_footprint.json --acl public-read
fi

rm -f ${intermediate}*

# 6. create thumbnail
>&2 echo "Generating thumbnail..."
thumb=${base}_thumb.png
to_clean+=($thumb ${thumb}.aux.xml)
info=$(rio info $vrt 2> /dev/null)
height=$(jq .height <<< $info)
width=$(jq .width <<< $info)
target_pixel_area=$(bc -l <<< "$THUMBNAIL_SIZE * 1000 / 0.75")
ratio=$(bc -l <<< "sqrt($target_pixel_area / ($width * $height))")
target_width=$(printf "%.0f" $(bc -l <<< "$width * $ratio"))
target_height=$(printf "%.0f" $(bc -l <<< "$height * $ratio"))
gdal_translate -of png $vrt $thumb -outsize $target_width $target_height
aws s3 cp $thumb ${output}_thumb.png --acl public-read
rm -f $vrt $thumb

if [ "$mask" -eq 1 ]; then
  # 7. create and upload warped VRT
  >&2 echo "Generating warped VRT..."
  warped_vrt=${base}_warped.vrt
  to_clean+=($warped_vrt)
  make_vrt.sh -r lanczos ${output}.tif > $warped_vrt
  aws s3 cp $warped_vrt ${output}_warped.vrt --acl public-read

  # 8. create and upload warped VRT for mask
  >&2 echo "Generating warped VRT for mask..."
  make_mask_vrt.py $warped_vrt | aws s3 cp - ${output}_warped_mask.vrt --acl public-read
else
  # 7. create and upload warped VRT
  >&2 echo "Generating warped VRT..."
  warped_vrt=${base}_warped.vrt
  to_clean+=($warped_vrt)
  make_vrt.sh -r lanczos -a ${output}.tif > $warped_vrt
  aws s3 cp $warped_vrt ${output}_warped.vrt --acl public-read
fi

rm -f $warped_vrt

# 9. create and upload metadata
>&2 echo "Generating metadata..."
if [ "$mask" -eq 1 ]; then
  get_metadata.py --include-mask  $output | aws s3 cp - ${output}.json --acl public-read
#  get_metadata.py --include-mask "${args[@]}" $output | aws s3 cp - ${output}.json --acl public-read
else
  get_metadata.py  $output | aws s3 cp - ${output}.json --acl public-read
#  get_metadata.py "${args[@]}" $output | aws s3 cp - ${output}.json --acl public-read
fi

# 10. Upload OIN metadata
aws s3 cp - ${output}_meta.json --acl public-read <<< $metadata
