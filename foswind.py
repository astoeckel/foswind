#!/usr/bin/env python3
"""
    foswind - Yet Another Boring Static Website Generator
    Copyright (C) 2017  Andreas Stöckel

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import os
import sys
import shlex
import logging
import subprocess
import tempfile
import random
import string

# Setup logging
logger = logging.getLogger("foswind")
logging.basicConfig(level=logging.INFO)

# Setup the command line parser
parser = argparse.ArgumentParser(
    description='Runs the foswind static website generator. Builds a series ' +
    'of HTML files from Markdown using pandoc, scans for dependencies, ' +
    'minifies images, gzips content and creates a directory, ready to be ' +
    'rsynced to the webserver.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument(
    '--source',
    default="published/",
    required=False,
    help='The source directory')
parser.add_argument(
    '--target',
    default="build-published/",
    required=False,
    help='The target directory')
parser.add_argument(
    '--template',
    default="static/layout.tpl",
    required=False,
    help='The template file that should be used when generating the content')
parser.add_argument(
    '--content-dir',
    default="content/",
    required=False,
    help='Source content root path for resolving local media links (photo, audio)')
parser.add_argument(
    '--static-dir',
    default="static/",
    required=False,
    help='Source content root path for resolving static files such as fonts and stylesheets')


def find_all_files(path, flt=lambda _: True, include_dirs=False):
    """
    Helper function which recursively finds all files in the given path which
    match the filter.
    """
    path_stack = [path]
    visited_paths = set()
    result = []
    while len(path_stack) > 0:
        # Fetch the current path from the stack
        cur_path = path_stack.pop()

        # Just to make sure we're not infinitely recursing through some
        # symlinks...
        real_path =  os.path.realpath(cur_path)
        if real_path in visited_paths:
            continue
        visited_paths.add(real_path)

        # List all files in the current path
        for f in map(lambda f: os.path.join(cur_path, f), os.listdir(cur_path)):
            if os.path.isfile(f) and flt(f):
                result.append(os.path.relpath(f, path))
            elif os.path.isdir(f):
                path_stack.append(f)
                if include_dirs:
                    result.append(os.path.relpath(f, path))
    return result


def get_source_files(source):
    return find_all_files(source, lambda f: f.endswith(".md"))


def build_makefile_entry(target, dependencies, commands):
    return target, shlex.quote(target), shlex.quote(target) + ": " + " ".join(map(shlex.quote, dependencies)) + \
        "\n\t" + \
        "\n\t".join(
            map(lambda c: c[0] + " " + " ".join(map(shlex.quote, c[1])), commands)) + "\n"


def build_makefile(entries):
    return ".PHONY: all\n\nall: " + " ".join(map(lambda f: shlex.quote(
        f[1]), entries)) + "\n\n" + "\n".join(map(lambda e: e[2], entries))


def build_pandoc_command(src, tar, template):
    return [
        "-f", "markdown",
        "-t", "html5",
        "-o", tar,
        "-s",
        "--mathml",
        "--template=" + template,
        "--smart",
        src
    ]


def build_convert_command(src, tar):
    return [
        src,
        "-resize", "921600@>",
        "-strip",
        tar
    ]


def build_guetzli_command(src, tar):
    return [
        "--quality", "84",
        src,
        tar
    ]


def build_gzip_command(src):
    return ["-f", "-k", "-9", src]


def generate_first_stage_makefile(source_path, target_path, template):
    entries = []
    for source_file in get_source_files(argparser.source):
        # Remove the source file extension
        source_file_noext, source_ext = os.path.splitext(source_file)

        # Assemble the source and the target filename
        src = os.path.join(argparser.source, source_file)
        tar_html = os.path.join(argparser.target, source_file_noext + ".html")
        tar_gz = os.path.join(argparser.target, source_file_noext + ".html.gz")

        # Build the makefile entries
        entries.append(
            build_makefile_entry(
                tar_html,
                [__file__, argparser.template, src],
                [("mkdir", ["-p", os.path.dirname(tar_html)]),
                 ("pandoc", build_pandoc_command(
                     src,
                     tar_html, argparser.template))]))

    # Return the entries constituting the first stage makefile
    return entries


def run_makefile(entries):
    """
    Runs the given makefile entries by spawning a "make" subprocess
    and piping the generated makefile in. Throws an exception if an
    error occurs.
    """

    import multiprocessing

    j = multiprocessing.cpu_count()

    res = subprocess.run(["make", "-j" + str(j), "-f", "-"],
                         input=bytes(build_makefile(entries), "utf-8"),
                         stderr=subprocess.PIPE)
    if res.returncode != 0:
        raise Exception(str(res.stderr, "utf-8"))


def scan_single_html_dependencies(html_file):
    from html.parser import HTMLParser

    # Map between tag names and attributes pointing at dependencies
    tag_attr_map = {
        "a": ["href"],
        "link": ["href"],
        "script": ["src"],
        "img": ["src"],
        "audio": ["src"],
        "video": ["src"]
    }

    # Result list
    dependencies = []

    # Class used to parse the dependencies from the HTML
    class LinkParser(HTMLParser):

        def handle_starttag(self, tag, attrs):
            if tag in tag_attr_map:
                for key, value in attrs:
                    if key in tag_attr_map[tag]:
                        dependencies.append(value)

    # Read the file
    parser = LinkParser()
    with open(html_file) as fin:
        for line in fin:
            parser.feed(line)
    return dependencies


def scan_html_dependencies(html_targets):
    local_dependencies = set()
    remote_dependencies = set()
    for tar_html in html_targets:
        for dependency in scan_single_html_dependencies(tar_html):
            dependency = dependency.split("#")[0]
            if len(dependency) == 0 or dependency.startswith("mailto:"):
                continue
            elif dependency.startswith("http:") or dependency.startswith(
                    "https:") or dependency.startswith("ftp:"):
                remote_dependencies.add(dependency)
            else:
                local_dependencies.add(dependency)
    return local_dependencies, remote_dependencies


def resolve_local_dependencies(dependencies, target, search_paths):
    result = []
    for dep in dependencies:
        tar = os.path.join(target, dep)
        found = False
        for search_path in search_paths:
            src = os.path.join(search_path, dep)
            if os.path.isfile(src):
                result.append((src, tar))
                found = True
                break
        if not found:
            raise Exception("Cannot resolve dependency \"" + dep + "\"")
    return result


def generate_dependency_makefile(resolved_dependencies):
    def random_str(N):
        return "".join(random.choice(string.ascii_lowercase +
                                     string.digits) for _ in range(N))

    entries = []
    for src, tar in resolved_dependencies:
        if tar.lower().endswith(".jpg") or tar.lower().endswith(".jpeg"):
            tmp_tar = tar + "." + random_str(8) + ".png"
            entries.append(build_makefile_entry(
                tar,
                [src],
                [("mkdir", ["-p", os.path.dirname(tar)]),
                 ("convert", build_convert_command(src, tmp_tar)),
                 ("guetzli", build_guetzli_command(tmp_tar, tar)),
                 ("rm", [tmp_tar])
                 ]
            ))
        else:
            entries.append(build_makefile_entry(
                tar,
                [__file__, src],
                [("mkdir", ["-p", os.path.dirname(tar)]),
                 ("cp", [src, tar])]
            ))
    return entries

def find_stale_files(path, expected_files):
    # Include all subdirectories in the expected_files set
    file_set = set()
    for expected_file in expected_files:
        expected_file = os.path.relpath(expected_file, path)
        while expected_file != '':
            file_set.add(expected_file)
            expected_file, _ = os.path.split(expected_file)

    # Remove all files which are in expected_files
    return filter(lambda f: not f in file_set,
            find_all_files(path, include_dirs=True)[::-1])

# Parse the arguments and check them for validity
argparser = parser.parse_args()

try:
    if not os.path.exists(argparser.target):
        os.makedirs(argparser.target)
    elif not os.path.isdir(argparser.target):
        raise Exception("Target must be a directory!")
    if not os.path.isdir(argparser.source):
        raise Exception("Source must be a directory!")

    logger.info("Building HTML from Markdown...")
    html_targets = generate_first_stage_makefile(
        argparser.source,
        argparser.target,
        argparser.template)
    run_makefile(html_targets)

    logger.info("Scanning HTML dependencies...")
    local_dependencies, _ = scan_html_dependencies(
        map(lambda x: x[0], html_targets))

    logger.info("Resolving dependencies...")
    resolved_dependencies = resolve_local_dependencies(
        local_dependencies, argparser.target, [
            argparser.static_dir, argparser.content_dir, argparser.target])

    logger.info("Copying and transforming media files...")
    dependency_targets = generate_dependency_makefile(resolved_dependencies)
    run_makefile(dependency_targets)

    logger.info("Scanning for stale files...")
    stale_files = find_stale_files(
        argparser.target,
        list(map(lambda x: x[0], html_targets)) +
        list(map(lambda x: x[0], dependency_targets)))
    for stale_file in stale_files:
        f = os.path.join(argparser.target, stale_file)
        logger.info("Deleting \"" + shlex.quote(f) + "\"")
        if os.path.isdir(f):
            os.rmdir(f)
        else:
            os.remove(f)

    logger.info("Done!")

except:
    logger.exception("")

