#ifndef POP_JPEG_H
#define POP_JPEG_H

#include <stddef.h>
#include <stdint.h>

/* Decode JPEG to packed RGB. *rgb is malloc'd. */
int jpeg_decode_rgb(const uint8_t *jpg, size_t len, uint8_t **rgb, int *w, int *h);
/* Encode packed RGB to JPEG at quality 1..100. *out is malloc'd (free with free()). */
int jpeg_encode_rgb(const uint8_t *rgb, int w, int h, int quality, uint8_t **out, size_t *len);

#endif
