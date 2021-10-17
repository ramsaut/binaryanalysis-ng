import os
import re
import mmap
import logging
from operator import itemgetter
from unpack_directory import *
from UnpackParser import SynthesizingParser, ExtractingParser, PaddingParser
from UnpackParserException import UnpackParserException
import bangsignatures

class ScanJob:
    def __init__(self, path):
        self._path = path
        self._unpack_directory = None

    def set_scan_environment(self, scan_environment):
        self.scan_environment = scan_environment

    @property
    def unpack_directory(self):
        if self._unpack_directory is None:
            self._unpack_directory = UnpackDirectory.from_ud_path(self.scan_environment.unpackdirectory, self._path)
        return self._unpack_directory

def is_unscannable(path):
    # TODO: do we want labels on unscannable files?
    return not path.is_file() or path.stat().st_size == 0
    # return path.is_dir() or path.is_fifo() or path.is_socket() or path.is_block_device() or path.is_char_device() or path.is_symlink()

def is_padding(path):
    validpadding = [b'\x00', b'\xff']
    ispadding = False

    with path.open('rb') as f:
        c = f.read(1)
        padding_char = c
        ispadding = c in validpadding
        if ispadding:
            while c == padding_char:
                c = f.read(1)
            ispadding = c == b''
    return ispadding

def extract_file(path_unpack_directory, in_file, offset, file_size):
    extracted_path = path_unpack_directory.extracted_filename(offset, file_size)
    abs_extracted_path = path_unpack_directory.unpack_root / extracted_path
    abs_extracted_path.parent.mkdir(parents=True, exist_ok=True)
    with abs_extracted_path.open('wb') as extracted_file:
        os.sendfile(extracted_file.fileno(), in_file.fileno(), offset, file_size)
    ud = UnpackDirectory(path_unpack_directory.unpack_root, None, False)
    ud.file_path = extracted_path
    path_unpack_directory.add_extracted_file(ud)
    return ud


def check_for_padding(path_unpack_directory):
    with path_unpack_directory.abs_file_path.open('rb') as in_file:
        try:
            unpack_parser = PaddingParser(in_file, 0)
            unpack_parser.parse_from_offset()
            logging.debug(f'check_for_padding: size = {unpack_parser.parsed_size}/{path_unpack_directory.size}')
            if unpack_parser.parsed_size == path_unpack_directory.size:
                logging.debug(f'check_for_padding: yield {unpack_parser} for {path_unpack_directory.file_path}')
                path_unpack_directory.unpack_parser = unpack_parser
                yield path_unpack_directory
        except UnpackParserException as e:
            logging.debug(f'check_for_padding: parser exception: {e}')

        logging.debug(f'check_for_padding: {path_unpack_directory.file_path} is not a padding file')

def find_extension_parsers(scan_environment, mapped_file):
    for ext, unpack_parsers in scan_environment.get_unpackparsers_for_extensions().items():
        for unpack_parser_cls in unpack_parsers:
            logging.debug(f'find_extension_parser: {ext!r} parsed by {unpack_parser_cls}')
            yield ext, unpack_parser_cls

def check_by_extension(scan_environment, path_unpack_directory):
    with path_unpack_directory.abs_file_path.open('rb') as in_file:
        mapped_file = mmap.mmap(in_file.fileno(),0, access=mmap.ACCESS_READ)
        for ext, unpack_parser_cls in find_extension_parsers(scan_environment, mapped_file):
            if bangsignatures.matches_file_pattern(path_unpack_directory.file_path, ext):
                logging.debug(f'check_by_extension: {unpack_parser_cls} parses extension {ext}')
                try:
                    unpack_parser = unpack_parser_cls(mapped_file, 0)
                    unpack_parser.parse_from_offset()
                    if unpack_parser.parsed_size == mapped_file.size():
                        logging.debug(f'check_by_extension: parser parsed entire file')
                        path_unpack_directory.unpack_parser = unpack_parser
                        yield path_unpack_directory
                    else:
                        logging.debug(f'check_by_extension: parser parsed [0:{unpack_parser.parsed_size}], leaving [{unpack_parser.parsed_size}:{mapped_file.size()}] ({mapped_file.size() - unpack_parser.parsed_size} bytes)')
                        # yield the path_unpack_directory with a ExtractingUnpackParser, in
                        # case we want to record metadata about it.
                        path_unpack_directory.unpack_parser = ExtractingParser.with_parts(
                            mapped_file,
                            [ (0,unpack_parser.parsed_size),
                            (unpack_parser.parsed_size, mapped_file.size() - unpack_parser.parsed_size) ]
                            )
                        yield path_unpack_directory

                        # yield the matched part of the file
                        ud = extract_file(path_unpack_directory, in_file, 0, unpack_parser.parsed_size)
                        ud.unpack_parser = unpack_parser
                        yield ud

                        # yield a synthesized file
                        ud = extract_file(path_unpack_directory, in_file, unpack_parser.parsed_size, mapped_file.size() - unpack_parser.parsed_size)
                        ud.unpack_parser = SynthesizingParser.with_size(mapped_file, unpack_parser.parsed_size, mapped_file.size() - unpack_parser.parsed_size)
                        yield ud

                        # stop after first successful extension parse
                        # TODO: make this configurable?
                        return

                except UnpackParserException as e:
                    logging.debug(f'check_by_extension: parser exception: {e}')


def find_offsets_for_signature(signature, unpack_parsers, mapped_file):
    s_offset, s_text = signature
    for r in re.finditer(re.escape(s_text), mapped_file):
        logging.debug(f'find_offsets_for_signature: match for {s_text!r} at {r.start()}, offset={s_offset}')
        if r.start() < s_offset:
            continue
        # TODO: prescan? or let the unpack_parser.parse() handle that?
        for u in unpack_parsers:
            yield r.start() - s_offset, u

def find_signature_parsers(scan_environment, mapped_file):
    for s, unpack_parsers in scan_environment.get_unpackparsers_for_signatures().items():
        logging.debug(f'find_signature_parsers: {s} parsed by {unpack_parsers}')
        # find offsets for signature
        for offset, unpack_parser in find_offsets_for_signature(s, unpack_parsers, mapped_file):
            yield offset, unpack_parser

def scan_signatures(scan_environment, mapped_file):
    scan_offset = 0
    for offset, unpack_parser_cls in sorted(find_signature_parsers(scan_environment, mapped_file), key=itemgetter(0)):
        logging.debug(f'scan_signatures: at {scan_offset}, found parser at {offset}, {unpack_parser_cls}')
        if offset < scan_offset: # we have passed this point in the file, ignore the result
            logging.debug(f'scan_signatures: skipping [{offset}:{scan_offset}]')
            continue
        # try if the unpackparser works
        try:
            logging.debug(f'scan_signatures: try parse at {offset} with {unpack_parser_cls}')
            unpack_parser = unpack_parser_cls(mapped_file, offset)
            unpack_parser.parse_from_offset()
            if offset == 0 and unpack_parser.parsed_size == mapped_file.size():
                logging.debug(f'scan_signatures: skipping [{scan_offset}:{unpack_parser.parsed_size}], covers entire file')
                return
            if offset > scan_offset:
                # if it does, yield a synthesizing parser for the padding before the file
                logging.debug(f'scan_signatures: [{scan_offset}:{offset}] yields SynthesizingParser, length {offset - scan_offset}')
                yield scan_offset, SynthesizingParser.with_size(mapped_file, offset, offset - scan_offset)
            # yield the part that the unpackparser parsed
            logging.debug(f'scan_signatures: [{offset}:{offset+unpack_parser.parsed_size}] yields {unpack_parser}, length {unpack_parser.parsed_size}')
            yield offset, unpack_parser
            scan_offset = offset + unpack_parser.parsed_size
        except UnpackParserException as e:
            logging.debug(f'scan_signatures: parser exception: {e}')
    # yield the trailing part
    if 0 < scan_offset < mapped_file.size():
        logging.debug(f'scan_signatures: [{scan_offset}:{mapped_file.size()}] yields SynthesizingParser, length {mapped_file.size() - scan_offset}')
        yield scan_offset, SynthesizingParser.with_size(mapped_file, offset, mapped_file.size() - scan_offset)


def check_by_signature(scan_environment, path_unpack_directory):
    # find offsets
    with path_unpack_directory.abs_file_path.open('rb') as in_file:
        mapped_file = mmap.mmap(in_file.fileno(),0, access=mmap.ACCESS_READ)
        parts = [] # record the parts for the ExtractingParser
        for offset, unpack_parser in scan_signatures(scan_environment, mapped_file):
            logging.debug(f'check_by_signature: got match at {offset}: {unpack_parser} length {unpack_parser.parsed_size}')
            if offset == 0 and unpack_parser.parsed_size == path_unpack_directory.size:
                # no need to extract a subfile
                path_unpack_directory.unpack_parser = unpack_parser
                yield path_unpack_directory
            else:
                ud = extract_file(path_unpack_directory, in_file, offset, unpack_parser.parsed_size)
                ud.unpack_parser = unpack_parser
                yield ud
                parts.append((offset, unpack_parser.parsed_size))
            # yield ExtractingParser
            if parts != []:
                path_unpack_directory.unpack_parser = ExtractingParser.with_parts(mapped_file, parts)
                yield path_unpack_directory

def process_job(scanjob):
    # scanjob has: path, unpack_directory object and context
    unpack_directory = scanjob.unpack_directory

    if is_unscannable(unpack_directory.file_path):
        return

    # if scanjob.context_is_padding(unpack_directory.context): return
    for ud in check_for_padding(unpack_directory):
        # TODO: implement processing from r.unpack_parser (PaddingParser)
        logging.debug(f'process padding file in {ud} with {ud.unpack_parser}')
        ud.unpack_parser.write_info(ud)
        return # skip padding file by returning

    for ud in check_by_extension(scanjob.scan_environment, unpack_directory):
        logging.debug(f'process_job: analyzing {ud.file_path} into {ud.ud_path} with {ud.unpack_parser}')
        # ud is an unpack_directory
        # unpackdirectory has files to unpack (i.e. for archives)
        for unpacked_dir in ud.unpack_parser.unpack(ud):
            logging.debug(f'process_job: unpacked {unpacked_dir.file_path}, with info in {unpacked_dir.ud_path}')
            # queue up
            pass
        ud.unpack_parser.write_info(ud)

    # stop after first successful unpack (TODO: make configurable?)
    if unpack_directory.is_scanned():
        return

    for ud in check_by_signature(scanjob.scan_environment, unpack_directory):
        logging.debug(f'process_job: analyzing {ud.file_path} into {ud.ud_path} with {ud.unpack_parser}')
        # if ud is synthesized, queue it for extra checks?
        for unpacked_dir in ud.unpack_files():
            # queue
            pass
        ud.unpack_parser.write_info(ud)

    # stop after first successful unpack (TODO)
    if unpack_directory.is_scanned():
        return

    # if extension and signature did not give any results, try other things
    # TODO: try featureless parsers

def process_jobs(scan_environment):
    # code smell, should not be needed if unpackparsers behave
    os.chdir(scan_environment.unpackdirectory)

    while True:
        try:
            # TODO: check if timeout long enough
            scanjob = scan_environment.scanfilequeue.get(timeout=86400)
        except scan_environment.scanfilequeue.Empty as e:
            break
        scanjob.set_scan_environment(scan_environment)
        process_job(scanjob)
        scan_environment.scanfilequeue.task_done()


