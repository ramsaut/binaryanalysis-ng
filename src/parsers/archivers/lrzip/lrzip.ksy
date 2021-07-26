meta:
  id: lrzip
  title: LRZIP
  license: CC0-1.0
  endian: le
doc-ref:
  - https://github.com/ckolivas/lrzip/blob/master/doc/magic.header.txt
seq:
  - id: magic
    contents: "LRZI"
  - id: major_version
    type: u1
  - id: minor_version
    type: u1
  - id: size_or_salt
    type: u8
  - id: unused1
    size: 2
  - id: lzma_properties
    type: u1
  - id: lzma_dictionary
    type: u4
  - id: md5_stored
    type: u1
  - id: encrypted
    type: u1
    valid:
      any-of: [0, 1]
  - id: unused2
    size: 1
  - id: rchunks
    type: rchunk
    repeat: expr
    repeat-expr: 1
types:
  rchunk:
    seq:
      - id: dummy
        type: u1
