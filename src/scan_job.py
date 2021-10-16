import os
import re
import mmap
import logging
from operator import itemgetter
from unpack_directory import *
from UnpackParser import SynthesizingParser
from UnpackParserException import UnpackParserException

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
    return not path.is_file()
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

def check_for_padding(path_unpack_directory):
    r = is_padding(path_unpack_directory.abs_file_path)    
    if r:
        # TODO: mark path_unpack_directory as padding?
        yield path_unpack_directory

def scan_extensions(scan_environment, path):
    # TODO: implement
    if False:
        yield None
    pass

def check_by_extension(scan_environment, path_unpack_directory):
    for unpack_parser in scan_extensions(scan_environment, path_unpack_directory.abs_file_path):
        # this will give all unpack_parsers that match the extension
        try:
            unpack_parser.parse_from_offset(path_unpack_directory.path, 0)
            unpack_parser.unpack(path_unpack_directory)
            # TODO: update info, not overwite
            unpack_parser.write_info(path_unpack_directory)
            yield path_unpack_directory
            # take the first working parser
            return
        except UnpackParserException as e:
            pass

def find_offsets_for_signature(signature, unpack_parsers, mapped_file):
    s_offset, s_text = signature
    for r in re.finditer(re.escape(s_text), mapped_file):
        logging.debug(f'find_offsets_for_signature: match for {s_text!r} at {r.start()}, offset={s_offset}')
        if r.start() < s_offset:
            continue
        # TODO: prescan?
        for u in unpack_parsers:
            yield r.start() - s_offset, u

def find_signatures(scan_environment, mapped_file):
    for s, unpack_parsers in scan_environment.get_unpackparsers_for_signatures().items():
        logging.debug(f'find_signatures: {s} parsed by {unpack_parsers}')
        # find offsets for signature
        for offset, unpack_parser in find_offsets_for_signature(s, unpack_parsers, mapped_file):
            yield offset, unpack_parser

def scan_signatures(scan_environment, mapped_file):
    scan_offset = 0
    for offset, unpack_parser_cls in sorted(find_signatures(scan_environment, mapped_file), key=itemgetter(0)):
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
                # if it does, return a synthesizing parser for the padding before the file
                logging.debug(f'scan_signatures: [{scan_offset}:{offset}] yields SynthesizingParser, length {offset - scan_offset}')
                yield scan_offset, SynthesizingParser.with_size(mapped_file, offset, offset - scan_offset)
            # return the unpackparser
            logging.debug(f'scan_signatures: [{offset}:{offset+unpack_parser.parsed_size}] yields {unpack_parser}, length {unpack_parser.parsed_size}')
            yield offset, unpack_parser
            scan_offset = offset + unpack_parser.parsed_size
        except UnpackParserException as e:
            logging.debug(f'scan_signatures: parser exception: {e}')
            pass
    # return the trailing part
    if 0 < scan_offset < mapped_file.size():
        logging.debug(f'scan_signatures: [{scan_offset}:{mapped_file.size()}] yields SynthesizingParser, length {mapped_file.size() - scan_offset}')
        yield scan_offset, SynthesizingParser.with_size(mapped_file, offset, mapped_file.size() - scan_offset)


def check_by_signature(scan_environment, path_unpack_directory):
    # find offsets
    with path_unpack_directory.file_path.open('rb') as in_file:
        mm = mmap.mmap(in_file.fileno(),0, access=mmap.ACCESS_READ)
        for offset, unpack_parser in scan_signatures(scan_environment, mm):
            logging.debug(f'check_by_signature: got match at {offset}: {unpack_parser} length {unpack_parser.parsed_size}')
            try:
                if offset == 0 and unpack_parser.parsed_size == path_unpack_directory.size:
                    # no need to extract a subfile
                    # unpack_parser.unpack(path_unpack_directory)
                    path_unpack_directory.unpack_parser = unpack_parser
                    # TODO: update info, not overwite
                    unpack_parser.write_info(path_unpack_directory)
                    yield path_unpack_directory
                else:
                    extracted_path = path_unpack_directory.extracted_filename(offset, unpack_parser.parsed_size)
                    abs_extracted_path = path_unpack_directory.unpack_root / extracted_path
                    abs_extracted_path.parent.mkdir(parents=True, exist_ok=True)
                    with abs_extracted_path.open('wb') as extracted_file:
                        os.sendfile(extracted_file.fileno(), in_file.fileno(), offset, unpack_parser.parsed_size)
                    ud = UnpackDirectory(path_unpack_directory.unpack_root, None, False)
                    ud.file_path = extracted_path
                    # record the extracted file
                    path_unpack_directory.add_extracted_file(ud)

                    # unpack_parser.unpack(ud)
                    ud.unpack_parser = unpack_parser
                    # TODO: update info, not overwite
                    unpack_parser.write_info(ud)
                    yield ud
            except UnpackParserException as e:
                pass

def process_job(scanjob):
    # scanjob has: path, unpack_directory object and context
    unpack_directory = scanjob.unpack_directory

    if is_unscannable(unpack_directory.file_path):
        return

    # if scanjob.context_is_padding(unpack_directory.context): return
    for r in check_for_padding(unpack_directory):
        return

    for r in check_by_extension(scanjob.scan_environment, unpack_directory):
        # r is an unpack_directory
        # unpackdirectory has files to unpack (i.e. for archives)
        # or extra data (which needs carving)
        for unpacked_dir in r.unpack_files():
            # queue up
            pass
        for extra in r.extra_data:
            # queue extra
            pass

    # stop after first successful unpack (TODO)
    # find some property on the unpack_directory
    print(unpack_directory.info)
    if unpack_directory.info != {}:
        return

    for r in check_by_signature(scanjob.scan_environment, unpack_directory):
        # if r is synthesized, queue it for extra checks?
        for unpacked_dir in r.unpack_files():
            # queue
            pass
        # for extra in r.extra_data: pass # no extra data if scanning by sig

    # stop after first successful unpack (TODO)
    if unpack_directory.info != {}:
        return

    # if extension and signature did not give any results, try other things
    # TODO

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

