import json
import os
import re
import shlex
import sys
import tarfile

from conda_verify.common import (check_build_number, check_build_string,
                                 check_name, check_specs, check_version,
                                 get_python_version_specs)
from conda_verify.const import FIELDS, LICENSE_FAMILIES
from conda_verify.exceptions import PackageError, RecipeError
from conda_verify.utils import all_ascii, get_bad_seq, get_field, get_object_type


class CondaPackageCheck(object):
    def __init__(self, path):
        self.path = path
        self.archive = tarfile.open(self.path)
        self.dist = self.check_package_name(self.path)
        self.name, self.version, self.build = self.dist.rsplit('-', 2)
        self.paths = set(m.path for m in self.archive.getmembers())
        self.index = self.archive.extractfile('info/index.json').read()
        self.info = json.loads(self.index.decode('utf-8'))
        self.win_pkg = bool(self.info['platform'] == 'win')

    @staticmethod
    def check_package_name(path):
        path = os.path.basename(path)
        seq = get_bad_seq(path)
        if seq:
            raise PackageError("'%s' not allowed in file name '%s'" % (seq, path))
        if path.endswith('.tar.bz2'):
            return path[:-8]
        elif path.endswith('.tar'):
            return path[:-4]
        raise PackageError("did not expect filename: %s" % path)

    def check_duplicate_members(self):
        if len(self.archive.getmembers()) != len(self.paths):
            raise PackageError("duplicate members")

    def check_index_encoding(self):
        if not all_ascii(self.index, self.win_pkg):
            raise PackageError("non-ASCII in: info/index.json")

    def check_members(self):
        for m in self.archive.getmembers():
            if sys.version_info.major == 2:
                unicode_path = m.path.decode('utf-8')
            else:
                unicode_path = m.path.encode('utf-8')

            if not all_ascii(unicode_path):
                raise PackageError("non-ASCII path: %r" % m.path)

    def info_files(self):
        raw = self.archive.extractfile('info/files').read()
        if not all_ascii(raw, self.win_pkg):
            raise PackageError("non-ASCII in: info/files")
        lista = [p.strip() for p in raw.decode('utf-8').splitlines()]
        for p in lista:
            if p.startswith('info/'):
                raise PackageError("Did not expect '%s' in info/files" % p)

        seta = set(lista)
        if len(lista) != len(seta):
            raise PackageError('info/files: duplicates')

        listb = [m.path for m in self.archive.getmembers()
                 if not (m.path.startswith('info/') or m.isdir())]
        setb = set(listb)

        if seta == setb:
            return
        for p in sorted(seta | setb):
            if p not in seta:
                print('%r not in info/files' % p)
            if p not in setb:
                print('%r not in tarball' % p)
        raise PackageError("info/files")

    def no_hardlinks(self):
        for m in self.archive.getmembers():
            if m.islnk():
                raise PackageError('hardlink found: %s' % m.path)

    def not_allowed_files(self):
        not_allowed = {'conda-meta', 'conda-bld',
                       'pkgs', 'pkgs32', 'envs'}
        not_allowed_dirs = tuple(x + '/' for x in not_allowed)
        for p in self.paths:
            if (p.startswith(not_allowed_dirs) or
                    p in not_allowed or
                    p.endswith('/.DS_Store') or
                    p.endswith('~')):
                raise PackageError("directory or filename not allowed: "
                                   "%s" % p)
            if 'info/package_metadata.json' in p or 'info/link.json' in p:
                if self.info['subdir'] != 'noarch' and 'preferred_env' not in self.info:
                    raise PackageError("file not allowed: %s" % p)

    def index_json(self):
        for varname in 'name', 'version', 'build':
            if self.info[varname] != getattr(self, varname):
                raise PackageError("info/index.json for %s: %r != %r" %
                                   (varname, self.info[varname],
                                    getattr(self, varname)))
        bn = self.info['build_number']
        if not isinstance(bn, int):
            raise PackageError("info/index.json: invalid build_number: %s" %
                               bn)

        lst = [
            check_name(self.info['name']),
            check_version(self.info['version']),
            check_build_number(self.info['build_number']),
        ]
        lst.append(check_build_string(self.info['build']))
        for res in lst:
            if res:
                raise PackageError("info/index.json: %s" % res)

        depends = self.info.get('depends')
        if depends is None:
            raise PackageError("info/index.json: key 'depends' missing")
        res = check_specs(self.info['depends'])
        if res:
            raise PackageError("info/index.json: %s" % res)

        lf = self.info.get('license_family', self.info.get('license'))
        if lf not in LICENSE_FAMILIES:
            raise PackageError("wrong license family: %s" % lf)

    def no_bat_and_exe(self):
        bats = {p[:-4] for p in self.paths if p.endswith('.bat')}
        exes = {p[:-4] for p in self.paths if p.endswith('.exe')}
        both = bats & exes
        if both:
            raise PackageError("Both .bat and .exe files: %s" % both)

    def _check_has_prefix_line(self, line):
        line = line.strip()
        try:
            placeholder, mode, f = [x.strip('"\'') for x in
                                    shlex.split(line, posix=False)]
        except ValueError:
            placeholder, mode, f = '/<dummy>/<placeholder>', 'text', line

        if f not in self.paths:
            raise PackageError("info/has_prefix: target '%s' not in "
                               "package" % f)

        if mode == 'binary':
            if self.name == 'python':
                raise PackageError("binary placeholder not allowed in Python")
            if self.win_pkg:
                raise PackageError("binary placeholder not allowed on Windows")

            if len(placeholder) != 255:
                msg = ("info/has_prefix: binary placeholder not "
                       "255 bytes, but: %d" % len(placeholder))
                raise PackageError(msg)
        elif mode == 'text':
            pass
        else:
            raise PackageError("info/has_prefix: invalid mode")

    def has_prefix(self):
        for m in self.archive.getmembers():
            if m.path != 'info/has_prefix':
                continue
            if self.win_pkg:
                print("WARNING: %s" % m.path)
            data = self.archive.extractfile(m.path).read()
            if not all_ascii(data, self.win_pkg):
                raise PackageError("non-ASCII in: info/has_prefix")
            for line in data.decode('utf-8').splitlines():
                self._check_has_prefix_line(line)

    def warn_post_link(self):
        for p in self.paths:
            if p.endswith((
                    '-post-link.sh',  '-pre-link.sh',  '-pre-unlink.sh',
                    '-post-link.bat', '-pre-link.bat', '-pre-unlink.bat',
                    )):
                print("WARNING: %s" % p)

    def no_setuptools(self):
        for p in self.paths:
            if p.endswith('easy-install.pth'):
                raise PackageError("easy-install.pth file not allowed")

        if self.name in ('setuptools', 'distribute'):
            return
        for p in self.paths:
            if p.endswith(('MyPyPa-0.1.0-py2.5.egg',
                           'mytestegg-1.0.0-py3.4.egg')):
                continue
            if (p.endswith('.egg') or
                    'site-packages/pkg_resources' in p or
                    'site-packages/__pycache__/pkg_resources' in p or
                    p.startswith('bin/easy_install') or
                    p.startswith('Scripts/easy_install')):
                raise PackageError("file '%s' not allowed" % p)

    def no_easy_install_script(self):
        for m in self.archive.getmembers():
            if not m.name.startswith(('bin/', 'Scripts/')):
                continue
            if not m.isfile():
                continue
            data = self.archive.extractfile(m.path).read(1024)
            if b'EASY-INSTALL-SCRIPT' in data:
                raise PackageError("easy install script found: %s" % m.name)

    def no_pth(self):
        for p in self.paths:
            if p.endswith('.pth'):
                raise PackageError("found namespace .pth file '%s'" % p)

    def warn_pyo(self):
        if self.name == 'python':
            return
        for p in self.paths:
            if p.endswith('.pyo'):
                print("WARNING: .pyo file: %s" % p)

    def no_py_next_so(self):
        for p in self.paths:
            if p.endswith('.so'):
                root = p[:-3]
            elif p.endswith('.pyd'):
                root = p[:-4]
            else:
                continue
            for ext in '.py', '.pyc':
                if root + ext in self.paths:
                    print("WARNING: %s next to: %s" % (ext, p))

    def no_pyc_in_stdlib(self):
        if self.name in {'python', 'scons', 'conda-build'}:
            return
        for p in self.paths:
            if p.endswith('.pyc') and not 'site-packages' in p:
                raise PackageError(".pyc found in stdlib: %s" % p)

    def no_2to3_pickle(self):
        if self.name == 'python':
            return
        for p in self.paths:
            if ('lib2to3' in p and p.endswith('.pickle')):
                raise PackageError("found lib2to3 .pickle: %s" % p)

    def pyc_files(self):
        if 'py3' in self.build:
            return
        for p in self.paths:
            if ('/site-packages/' not in p) or ('/port_v3/' in p):
                continue
            if p.endswith('.py') and (p + 'c') not in self.paths:
                print("WARNING: pyc missing for:", p)

    def menu_names(self):
        menu_json_files = []
        for p in self.paths:
            if p.startswith('Menu/') and p.endswith('.json'):
                menu_json_files.append(p)
        if len(menu_json_files) == 0:
            pass
        elif len(menu_json_files) == 1:
            fn = menu_json_files[0][5:]
            if fn != '%s.json' % self.name:
                raise PackageError("wrong Menu json file name: %s" % fn)
        else:
            raise PackageError("too many Menu json files: %r" %
                               menu_json_files)

    def check_windows_arch(self):
        if self.name in ('python', 'conda-build', 'pip', 'xlwings',
                         'phantomjs', 'qt', 'graphviz', 'nsis', 'swig'):
            return
        if not self.win_pkg:
            return
        arch = self.info['arch']
        if arch not in ('x86', 'x86_64'):
            raise PackageError("Unrecognized Windows architecture: %s" %
                               arch)
        for m in self.archive.getmembers():
            if not m.name.lower().endswith(('.exe', '.dll')):
                continue
            data = self.archive.extractfile(m.path).read(4096)
            tp = get_object_type(data)
            if ((arch == 'x86' and tp != 'DLL I386') or
                (arch == 'x86_64' and tp != 'DLL AMD64')):
                raise PackageError("File %s has object type %s, but info/"
                                   "index.json arch is %s" %
                                   (m.name, tp, arch))

    def get_sp_location(self):
        py_ver = get_python_version_specs(self.info['depends'])
        if py_ver is None:
            return '<not a Python package>'

        if self.win_pkg:
            return 'Lib/site-packages'
        else:
            return 'lib/python%s/site-packages' % py_ver

    def list_packages(self):
        sp_location = self.get_sp_location()
        pat = re.compile(r'site-packages/([^/]+)')
        res = set()
        for p in self.paths:
            m = pat.search(p)
            if m is None:
                continue
            if not p.startswith(sp_location):
                print("WARNING: found %s" % p)
            fn = m.group(1)
            if '-' in fn or fn.endswith('.pyc'):
                continue
            res.add(fn)
        for pkg_name in 'numpy', 'scipy':
            if self.name != pkg_name and pkg_name in res:
                raise PackageError("found %s" % pkg_name)
        if self.name not in ('setuptools', 'distribute', 'python'):
            for x in ('pkg_resources.py', 'setuptools.pth', 'easy_install.py',
                      'setuptools'):
                if x in res:
                    raise PackageError("found %s" % x)


class CondaRecipeCheck(object):
    def __init__(self, meta, recipe_dir):
        self.meta = meta
        self.recipe_dir = recipe_dir
        self.name_pat = re.compile(r'[a-z0-9_][a-z0-9_\-\.]*$')
        self.version_pat = re.compile(r'[\w\.]+$')
        self.ver_spec_pat = re.compile(r'[\w\.,=!<>\*]+$')
        self.url_pat = re.compile(r'(ftp|http(s)?)://')
        self.hash_pat = {'md5': re.compile(r'[a-f0-9]{32}$'),
                         'sha1': re.compile(r'[a-f0-9]{40}$'),
                         'sha256': re.compile(r'[a-f0-9]{64}$')}

    def check_fields(self):
        meta = self.meta
        for section in meta:
            if section not in FIELDS:
                raise RecipeError("Unknown section: %s" % section)
            submeta = meta.get(section)
            if submeta is None:
                submeta = {}
            for key in submeta:
                if key not in FIELDS[section]:
                    raise RecipeError("in section %r: unknown key %r" %
                                      (section, key))

        for res in [
            check_name(get_field(meta, 'package/name')),
            check_version(get_field(meta, 'package/version')),
            check_build_number(get_field(meta, 'build/number', 0)),
            ]:
            if res:
                raise RecipeError(res)
    
    def check_requirements(self):
        meta = self.meta
        for req in (get_field(meta, 'requirements/build', []) +
                    get_field(meta, 'requirements/run', [])):
            parts = req.split()
            name = parts[0]
            if not self.name_pat.match(name):
                if req in get_field(meta, 'requirements/run', []):
                    raise RecipeError("invalid run requirement name '%s'" % name)
                else:
                    raise RecipeError("invalid build requirement name '%s'" % name)
        for field in 'requirements/build', 'requirements/run':
            specs = get_field(meta, field, [])
            res = check_specs(specs)
            if res:
                raise RecipeError(res)

    def check_url(self, url):
        if not self.url_pat.match(url):
            raise RecipeError("not a valid URL: %s" % url)

    def check_about(self):
        meta = self.meta
        summary = get_field(meta, 'about/summary')
        if summary and len(summary) > 80:
            msg = "summary exceeds 80 characters"
            raise RecipeError(msg)

        for field in ('about/home', 'about/dev_url', 'about/doc_url',
                      'about/license_url'):
            url = get_field(meta, field)
            if url:
                self.check_url(url)

    def check_source(self):
        meta = self.meta
        src = meta.get('source')
        if not src:
            return
        url = src.get('url')
        if url:
            self.check_url(url)

            for ht in 'md5', 'sha1', 'sha256':
                hexgigest = src.get(ht)
                if hexgigest and not self.hash_pat[ht].match(hexgigest):
                    raise RecipeError("invalid hash: %s" % hexgigest)

        git_url = src.get('git_url')
        if git_url and (src.get('git_tag') and src.get('git_branch')):
            raise RecipeError("cannot specify both git_branch and git_tag")

    def check_license_family(self):
        meta = self.meta
        lf = get_field(meta, 'about/license_family',
                       get_field(meta, 'about/license'))
        if lf not in LICENSE_FAMILIES:
            print("""\
        Error: license_family is invalid: %s
        Note that about/license_family falls back to about/license.
        Allowed license families are:""" % lf)
            for x in LICENSE_FAMILIES:
                print("  - %s" % x)
            raise RecipeError("wrong license family")

    def validate_files(self):
        meta = self.meta
        for field in 'test/files', 'source/patches', 'test/source_files':
            flst = get_field(meta, field)
            if not flst:
                continue
            for fn in flst:
                if fn.startswith('..'):
                    raise RecipeError("path outsite recipe: %s" % fn)
                path = os.path.join(self.recipe_dir, fn)
                if os.path.isfile(path):
                    continue
                raise RecipeError("no such file '%s'" % path)

    def check_dir_content(self):
        recipe_dir = self.recipe_dir
        disallowed_extensions = (
            '.tar', '.tar.gz', '.tar.bz2', '.tar.xz',
            '.so', '.dylib', '.la', '.a', '.dll', '.pyd',
        )
        for root, unused_dirs, files in os.walk(recipe_dir):
            for fn in files:
                fn_lower = fn.lower()
                path = os.path.join(root, fn)
                # only allow small archives for testing
                if (fn_lower.endswith(('.bz2', '.gz')) and
                            os.path.getsize(path) > 512):
                    raise RecipeError("found: %s (too large)" % fn)
                if fn_lower.endswith(disallowed_extensions):
                    raise RecipeError("found: %s" % fn)

        # check total size od recipe directory (recursively)
        kb_size = self.dir_size(recipe_dir) / 1024
        kb_limit = 512
        if kb_size > kb_limit:
            raise RecipeError("recipe too large: %d KB (limit %d KB)" %
                              (kb_size, kb_limit))

    @staticmethod
    def dir_size(dir_path):
        return sum(sum(os.path.getsize(os.path.join(root, fn)) for fn in files)
                   for root, unused_dirs, files in os.walk(dir_path))
