# -*- coding: utf-8 -*-
# Copyright 2010 Dirk Holtwick, holtwick.it
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import logging
import mimetypes
import os.path
import re
import reportlab
import shutil
import sys
import tempfile
import gzip

from functools import wraps
from io import UnsupportedOperation

from six import binary_type, StringIO

from reportlab.lib.colors import Color, toColor
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.lib.units import inch, cm

try:
    import httplib
except ImportError:
    import http.client as httplib
try:
    from urllib2 import urlopen, HTTPError
except ImportError:
    from urllib.request import urlopen, HTTPError
try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse
try:
    from urllib import splithost
except ImportError:
    from urllib.parse import splithost

rgb_re = re.compile("^.*?rgb[(]([0-9]+).*?([0-9]+).*?([0-9]+)[)].*?[ ]*$")

_reportlab_version = tuple(map(int, reportlab.Version.split('.')))
if _reportlab_version < (2, 1):
    raise ImportError("Reportlab Version 2.1+ is needed!")

REPORTLAB22 = _reportlab_version >= (2, 2)

log = logging.getLogger("xhtml2pdf")

try:
    import PyPDF2
except:
    PyPDF2 = None

try:
    from reportlab.graphics import renderPM
except:
    renderPM = None

try:
    from reportlab.graphics import renderSVG
except:
    renderSVG = None

# =========================================================================
# Memoize decorator
# =========================================================================
def memoized(fn):
    """
    A decorator-wrapper around `Memoized` that allows us to use @wraps so docstrings and such are preserved.

    :param fn:
    :return:
    """
    memoize = Memoized(fn)

    @wraps(fn)
    def helper(*args, **kwargs):
        return memoize(*args, **kwargs)
    return helper

class Memoized(object):
    """
    A kwargs-aware memoizer, better than the one in python :)

    Don't pass in too large kwargs, since this turns them into a tuple of
    tuples. Also, avoid mutable types (as usual for memoizers)

    What this does is to create a dictionary of {(*parameters):return value},
    and uses it as a cache for subsequent calls to the same method.
    It is especially useful for functions that don't rely on external variables
    and that are called often. It's a perfect match for our getSize etc...
    """

    def __init__(self, func):
        self.cache = {}
        self.func = func

    def __call__(self, *args, **kwargs):
        # Make sure the following line is not actually slower than what you're
        # trying to memoize
        if sys.version[0] == '2':
            args_plus = tuple(kwargs.items())
        else:
            args_plus = tuple(iter(kwargs.items()))
        key = (args, args_plus)
        try:
            if key not in self.cache:
                res = self.func(*args, **kwargs)
                self.cache[key] = res
            return self.cache[key]
        except TypeError:
            # happens if any of the parameters is a list
            return self.func(*args, **kwargs)


def format_error_message():
    """
    Helper to get a nice traceback as string
    """
    import traceback

    limit = None
    tb_type, tb_value, tb = sys.exc_info()
    tb_list = traceback.format_tb(tb, limit) + \
              traceback.format_exception_only(tb_type, tb_value)
    return "Traceback (innermost last):\n" + "%-20s %s" % ("".join(tb_list[: - 1]), tb_list[- 1])


def to_list(value):
    if type(value) not in (list, tuple):
        return [value]
    return list(value)


@memoized
def get_color(value, default=None):
    """
    Convert to color value.
    This returns a Color object instance from a text bit.
    """

    if isinstance(value, Color):
        return value
    value = str(value).strip().lower()
    if value == "transparent" or value == "none":
        return default
    if value in COLOR_BY_NAME:
        return COLOR_BY_NAME[value]
    if value.startswith("#") and len(value) == 4:
        value = "#" + value[1] + value[1] + \
                value[2] + value[2] + value[3] + value[3]
    elif rgb_re.search(value):
        # e.g., value = "<css function: rgb(153, 51, 153)>", go figure:
        r, g, b = [int(x) for x in rgb_re.search(value).groups()]
        value = "#%02x%02x%02x" % (r, g, b)
    else:
        # Shrug
        pass

    return toColor(value, default)  # Calling the reportlab function


def get_border_style(value, default=None):
    # log.debug(value)
    if value and (str(value).lower() not in ("none", "hidden")):
        return value
    return default


mm = cm / 10.0
dpi96 = (1.0 / 96.0 * inch)

_absolute_size_table = {
    "1": 50.0 / 100.0,
    "xx-small": 50.0 / 100.0,
    "x-small": 50.0 / 100.0,
    "2": 75.0 / 100.0,
    "small": 75.0 / 100.0,
    "3": 100.0 / 100.0,
    "medium": 100.0 / 100.0,
    "4": 125.0 / 100.0,
    "large": 125.0 / 100.0,
    "5": 150.0 / 100.0,
    "x-large": 150.0 / 100.0,
    "6": 175.0 / 100.0,
    "xx-large": 175.0 / 100.0,
    "7": 200.0 / 100.0,
    "xxx-large": 200.0 / 100.0,
}

_relative_size_table = {
    "larger": 1.25,
    "smaller": 0.75,
    "+4": 200.0 / 100.0,
    "+3": 175.0 / 100.0,
    "+2": 150.0 / 100.0,
    "+1": 125.0 / 100.0,
    "-1": 75.0 / 100.0,
    "-2": 50.0 / 100.0,
    "-3": 25.0 / 100.0,
}

MIN_FONT_SIZE = 1.0


@memoized
def get_size(value, relative=0, base=None, default=0.0):
    """
    Converts strings to standard sizes.
    That is the function taking a string of CSS size ('12pt', '1cm' and so on)
    and converts it into a float in a standard unit (in our case, points)::

        >>> get_size('12pt')
        12.0
        >>> get_size('1cm')
        28.346456692913385
    """
    original = value
    try:
        if value is None:
            return relative
        elif type(value) is float:
            return value
        elif isinstance(value, int):
            return float(value)
        elif type(value) in (tuple, list):
            value = "".join(value)
        value = str(value).strip().lower().replace(",", ".")
        if value[-2:] == 'cm':
            return float(value[:-2].strip()) * cm
        elif value[-2:] == 'mm':
            return float(value[:-2].strip()) * mm  # 1mm = 0.1cm
        elif value[-2:] == 'in':
            return float(value[:-2].strip()) * inch  # 1pt == 1/72inch
        elif value[-2:] == 'inch':
            return float(value[:-4].strip()) * inch  # 1pt == 1/72inch
        elif value[-2:] == 'pt':
            return float(value[:-2].strip())
        elif value[-2:] == 'pc':
            return float(value[:-2].strip()) * 12.0  # 1pc == 12pt
        elif value[-2:] == 'px':
            # XXX W3C says, use 96pdi
            # http://www.w3.org/TR/CSS21/syndata.html#length-units
            return float(value[:-2].strip()) * dpi96
        elif value[-1:] == 'i':  # 1pt == 1/72inch
            return float(value[:-1].strip()) * inch
        elif value in ("none", "0", "auto"):
            return 0.0
        elif relative:
            if value[-2:] == 'em':  # XXX
                # 1em = 1 * fontSize
                return float(value[:-2].strip()) * relative
            elif value[-2:] == 'ex':  # XXX
                # 1ex = 1/2 fontSize
                return float(value[:-2].strip()) * (relative / 2.0)
            elif value[-1:] == '%':
                # 1% = (fontSize * 1) / 100
                return (relative * float(value[:-1].strip())) / 100.0
            elif value in ("normal", "inherit"):
                return relative
            elif value in _relative_size_table:
                if base:
                    return max(MIN_FONT_SIZE, base * _relative_size_table[value])
                return max(MIN_FONT_SIZE, relative * _relative_size_table[value])
            elif value in _absolute_size_table:
                if base:
                    return max(MIN_FONT_SIZE, base * _absolute_size_table[value])
                return max(MIN_FONT_SIZE, relative * _absolute_size_table[value])
            else:
                return max(MIN_FONT_SIZE, relative * float(value))
        try:
            value = float(value)
        except:
            log.warn("getSize: Not a float %r", value)
            return default  # value = 0
        return max(0, value)
    except Exception:
        log.warn("getSize %r %r", original, relative, exc_info=1)
        return default


@memoized
def get_coordinates(x, y, w, h, pagesize):
    """
    As a stupid programmer I like to use the upper left
    corner of the document as the 0,0 coords therefore
    we need to do some fancy calculations
    """
    ax, ay = pagesize
    if x < 0:
        x = ax + x
    if y < 0:
        y = ay + y
    if w is not None and h is not None:
        if w <= 0:
            w = (ax - x + w)
        if h <= 0:
            h = (ay - y + h)
        return x, (ay - y - h), w, h
    return x, (ay - y)


@memoized
def get_box(box, pagesize):
    """
    Parse sizes by corners in the form:
    <X-Left> <Y-Upper> <Width> <Height>
    The last to values with negative values are interpreted as offsets form
    the right and lower border.
    """
    box = str(box).split()
    if len(box) != 4:
        raise Exception("box not defined right way")
    x, y, w, h = [get_size(pos) for pos in box]
    return get_coordinates(x, y, w, h, pagesize)


def get_frame_dimensions(data, page_width, page_height):
    """Calculate dimensions of a frame

    Returns left, top, width and height of the frame in points.
    """
    box = data.get("-pdf-frame-box", [])
    if len(box) == 4:
        return [get_size(x) for x in box]
    top = get_size(data.get("top", 0))
    left = get_size(data.get("left", 0))
    bottom = get_size(data.get("bottom", 0))
    right = get_size(data.get("right", 0))
    if "height" in data:
        height = get_size(data["height"])
        if "top" in data:
            top = get_size(data["top"])
            bottom = page_height - (top + height)
        elif "bottom" in data:
            bottom = get_size(data["bottom"])
            top = page_height - (bottom + height)
    if "width" in data:
        width = get_size(data["width"])
        if "left" in data:
            left = get_size(data["left"])
            right = page_width - (left + width)
        elif "right" in data:
            right = get_size(data["right"])
            left = page_width - (right + width)
    top += get_size(data.get("margin-top", 0))
    left += get_size(data.get("margin-left", 0))
    bottom += get_size(data.get("margin-bottom", 0))
    right += get_size(data.get("margin-right", 0))

    width = page_width - (left + right)
    height = page_height - (top + bottom)
    return left, top, width, height


@memoized
def get_position(position, pagesize):
    """
    Pair of coordinates
    """
    position = str(position).split()
    if len(position) != 2:
        raise Exception("position not defined right way")
    x, y = [get_size(pos) for pos in position]
    return get_coordinates(x, y, None, None, pagesize)


def str_to_bool(s):
    " Is it a boolean? "
    return str(s).lower() in ("y", "yes", "1", "true")


_uid = 0


def get_uid():
    """Unique ID"""
    global _uid
    _uid += 1
    return str(_uid)


_alignments = {
    "left": TA_LEFT,
    "center": TA_CENTER,
    "middle": TA_CENTER,
    "right": TA_RIGHT,
    "justify": TA_JUSTIFY,
}


def get_alignment(value, default=TA_LEFT):
    return _alignments.get(str(value).lower(), default)


class PisaTempFile(object):
    """
    A thin wrapper around `tempfile.SpooledTemporaryFile()`.

    Deprecated: Callers should use `SpooledTemporaryFile()` directly.
    """

    def __init__(self, buffer=None, mode="wb+", **kwargs):
        """
        Creates a TempFile object containing the specified buffer. If capacity is specified, we use a real temporary
        file once the file gets larger than that size. Otherwise, the data is stored in memory.
        """
        if buffer is None:
            self._delegate = tempfile.SpooledTemporaryFile(mode=mode)
        else:
            self._delegate = tempfile.SpooledTemporaryFile(buffer, mode=mode)

    @property
    def name(self):
        """
        Get a named temporary file
        """

        self._delegate.rollover()
        return str(self._delegate.name)

    def getvalue(self):
        """
        Get value of file.

        Work around to keep the strange properties of this object.
        """
        self._delegate.seek(0)
        return self._delegate.read()

    def __getattr__(self, name):
        try:
            return getattr(self._delegate, name)
        except AttributeError:
            # hide the delegation
            e = "object '%s' has no attribute '%s'" % (self.__class__.__name__, name)
            raise AttributeError(e)


class PisaFileObject:
    """
    XXX
    """
    _rx_datauri = re.compile("^data:(?P<mime>[a-z]+/[a-z]+);base64,(?P<data>.*)$", re.M | re.DOTALL)

    def __init__(self, uri, basepath=None):
        self.basepath = basepath
        self.mimetype = None
        self.file = None
        self.data = None
        self.uri = None
        self.local = None
        self.tmp_file = None
        uri = uri or str()
        if type(uri) != str:
            uri = uri.decode("utf-8")
        log.debug("FileObject %r, Basepath: %r", uri, basepath)

        # Data URI
        if uri.startswith("data:"):
            m = self._rx_datauri.match(uri)
            self.mimetype = m.group("mime")
            self.data = base64.decodestring(m.group("data"))

        else:
            # Check if we have an external scheme
            if basepath and not urlparse.urlparse(uri).scheme:
                urlParts = urlparse.urlparse(basepath)
            else:
                urlParts = urlparse.urlparse(uri)

            log.debug("URLParts: %r", urlParts)

            if urlParts.scheme == 'file':
                if basepath and uri.startswith('/'):
                    uri = urlparse.urljoin(basepath, uri[1:])
                url_response = urlopen(uri)
                self.mimetype = url_response.info().get(
                    "Content-Type", '').split(";")[0]
                self.uri = url_response.geturl()
                self.file = url_response

            # Drive letters have len==1 but we are looking
            # for things like http:
            elif urlParts.scheme in ('http', 'https'):

                # External data
                if basepath:
                    uri = urlparse.urljoin(basepath, uri)

                #path = urlparse.urlsplit(url)[2]
                #mimetype = getMimeType(path)

                # Using HTTPLIB
                server, path = splithost(uri[uri.find("//"):])
                if uri.startswith("https://"):
                    conn = httplib.HTTPSConnection(server)
                else:
                    conn = httplib.HTTPConnection(server)
                conn.request("GET", path)
                r1 = conn.getresponse()
                # log.debug("HTTP %r %r %r %r", server, path, uri, r1)
                if (r1.status, r1.reason) == (200, "OK"):
                    self.mimetype = r1.getheader(
                        "Content-Type", '').split(";")[0]
                    self.uri = uri
                    if r1.getheader("content-encoding") == "gzip":
                        self.file = gzip.GzipFile(
                            mode="rb", fileobj=StringIO(r1.read()))
                    else:
                        self.file = r1
                else:
                    try:
                        url_response = urlopen(uri)
                    except HTTPError:
                        return
                    self.mimetype = url_response.info().get(
                        "Content-Type", '').split(";")[0]
                    self.uri = url_response.geturl()
                    self.file = url_response

            else:

                # Local data
                if basepath:
                    uri = os.path.normpath(os.path.join(basepath, uri))

                if os.path.isfile(uri):
                    self.uri = uri
                    self.local = uri
                    self.set_mimetype_by_name(uri)
                    self.file = open(uri, "rb")

    def get_file(self):
        if self.file is not None:
            return self.file
        if self.data is not None:
            return PisaTempFile(self.data)
        return None

    def get_named_file(self):
        if self.not_found():
            return None
        if self.local:
            return str(self.local)
        if not self.tmp_file:
            self.tmp_file = tempfile.NamedTemporaryFile()
            if self.file:
                shutil.copyfileobj(self.file, self.tmp_file)
            else:
                self.tmp_file.write(self.get_data())
            self.tmp_file.flush()
        return self.tmp_file.name

    def get_data(self):
        if self.data is not None:
            return self.data
        if self.file is not None:
            self.data = self.file.read()
            return self.data
        return None

    def not_found(self):
        return (self.file is None) and (self.data is None)

    def set_mimetype_by_name(self, name):
        " Guess the mime type "
        mimetype = mimetypes.guess_type(name)[0]
        if mimetype is not None:
            self.mimetype = mimetypes.guess_type(name)[0].split(";")[0]


def get_file(*args, **kwargs):
    file = PisaFileObject(*args, **kwargs)
    if file.not_found():
        return None
    return file


COLOR_BY_NAME = {'activeborder': Color(212, 208, 200),
                 'activecaption': Color(10, 36, 106),
                 'aliceblue': Color(.941176, .972549, 1),
                 'antiquewhite': Color(.980392, .921569, .843137),
                 'appworkspace': Color(128, 128, 128),
                 'aqua': Color(0, 1, 1),
                 'aquamarine': Color(.498039, 1, .831373),
                 'azure': Color(.941176, 1, 1),
                 'background': Color(58, 110, 165),
                 'beige': Color(.960784, .960784, .862745),
                 'bisque': Color(1, .894118, .768627),
                 'black': Color(0, 0, 0),
                 'blanchedalmond': Color(1, .921569, .803922),
                 'blue': Color(0, 0, 1),
                 'blueviolet': Color(.541176, .168627, .886275),
                 'brown': Color(.647059, .164706, .164706),
                 'burlywood': Color(.870588, .721569, .529412),
                 'buttonface': Color(212, 208, 200),
                 'buttonhighlight': Color(255, 255, 255),
                 'buttonshadow': Color(128, 128, 128),
                 'buttontext': Color(0, 0, 0),
                 'cadetblue': Color(.372549, .619608, .627451),
                 'captiontext': Color(255, 255, 255),
                 'chartreuse': Color(.498039, 1, 0),
                 'chocolate': Color(.823529, .411765, .117647),
                 'coral': Color(1, .498039, .313725),
                 'cornflowerblue': Color(.392157, .584314, .929412),
                 'cornsilk': Color(1, .972549, .862745),
                 'crimson': Color(.862745, .078431, .235294),
                 'cyan': Color(0, 1, 1),
                 'darkblue': Color(0, 0, .545098),
                 'darkcyan': Color(0, .545098, .545098),
                 'darkgoldenrod': Color(.721569, .52549, .043137),
                 'darkgray': Color(.662745, .662745, .662745),
                 'darkgreen': Color(0, .392157, 0),
                 'darkgrey': Color(.662745, .662745, .662745),
                 'darkkhaki': Color(.741176, .717647, .419608),
                 'darkmagenta': Color(.545098, 0, .545098),
                 'darkolivegreen': Color(.333333, .419608, .184314),
                 'darkorange': Color(1, .54902, 0),
                 'darkorchid': Color(.6, .196078, .8),
                 'darkred': Color(.545098, 0, 0),
                 'darksalmon': Color(.913725, .588235, .478431),
                 'darkseagreen': Color(.560784, .737255, .560784),
                 'darkslateblue': Color(.282353, .239216, .545098),
                 'darkslategray': Color(.184314, .309804, .309804),
                 'darkslategrey': Color(.184314, .309804, .309804),
                 'darkturquoise': Color(0, .807843, .819608),
                 'darkviolet': Color(.580392, 0, .827451),
                 'deeppink': Color(1, .078431, .576471),
                 'deepskyblue': Color(0, .74902, 1),
                 'dimgray': Color(.411765, .411765, .411765),
                 'dimgrey': Color(.411765, .411765, .411765),
                 'dodgerblue': Color(.117647, .564706, 1),
                 'firebrick': Color(.698039, .133333, .133333),
                 'floralwhite': Color(1, .980392, .941176),
                 'forestgreen': Color(.133333, .545098, .133333),
                 'fuchsia': Color(1, 0, 1),
                 'gainsboro': Color(.862745, .862745, .862745),
                 'ghostwhite': Color(.972549, .972549, 1),
                 'gold': Color(1, .843137, 0),
                 'goldenrod': Color(.854902, .647059, .12549),
                 'gray': Color(.501961, .501961, .501961),
                 'graytext': Color(128, 128, 128),
                 'green': Color(0, .501961, 0),
                 'greenyellow': Color(.678431, 1, .184314),
                 'grey': Color(.501961, .501961, .501961),
                 'highlight': Color(10, 36, 106),
                 'highlighttext': Color(255, 255, 255),
                 'honeydew': Color(.941176, 1, .941176),
                 'hotpink': Color(1, .411765, .705882),
                 'inactiveborder': Color(212, 208, 200),
                 'inactivecaption': Color(128, 128, 128),
                 'inactivecaptiontext': Color(212, 208, 200),
                 'indianred': Color(.803922, .360784, .360784),
                 'indigo': Color(.294118, 0, .509804),
                 'infobackground': Color(255, 255, 225),
                 'infotext': Color(0, 0, 0),
                 'ivory': Color(1, 1, .941176),
                 'khaki': Color(.941176, .901961, .54902),
                 'lavender': Color(.901961, .901961, .980392),
                 'lavenderblush': Color(1, .941176, .960784),
                 'lawngreen': Color(.486275, .988235, 0),
                 'lemonchiffon': Color(1, .980392, .803922),
                 'lightblue': Color(.678431, .847059, .901961),
                 'lightcoral': Color(.941176, .501961, .501961),
                 'lightcyan': Color(.878431, 1, 1),
                 'lightgoldenrodyellow': Color(.980392, .980392, .823529),
                 'lightgray': Color(.827451, .827451, .827451),
                 'lightgreen': Color(.564706, .933333, .564706),
                 'lightgrey': Color(.827451, .827451, .827451),
                 'lightpink': Color(1, .713725, .756863),
                 'lightsalmon': Color(1, .627451, .478431),
                 'lightseagreen': Color(.12549, .698039, .666667),
                 'lightskyblue': Color(.529412, .807843, .980392),
                 'lightslategray': Color(.466667, .533333, .6),
                 'lightslategrey': Color(.466667, .533333, .6),
                 'lightsteelblue': Color(.690196, .768627, .870588),
                 'lightyellow': Color(1, 1, .878431),
                 'lime': Color(0, 1, 0),
                 'limegreen': Color(.196078, .803922, .196078),
                 'linen': Color(.980392, .941176, .901961),
                 'magenta': Color(1, 0, 1),
                 'maroon': Color(.501961, 0, 0),
                 'mediumaquamarine': Color(.4, .803922, .666667),
                 'mediumblue': Color(0, 0, .803922),
                 'mediumorchid': Color(.729412, .333333, .827451),
                 'mediumpurple': Color(.576471, .439216, .858824),
                 'mediumseagreen': Color(.235294, .701961, .443137),
                 'mediumslateblue': Color(.482353, .407843, .933333),
                 'mediumspringgreen': Color(0, .980392, .603922),
                 'mediumturquoise': Color(.282353, .819608, .8),
                 'mediumvioletred': Color(.780392, .082353, .521569),
                 'menu': Color(212, 208, 200),
                 'menutext': Color(0, 0, 0),
                 'midnightblue': Color(.098039, .098039, .439216),
                 'mintcream': Color(.960784, 1, .980392),
                 'mistyrose': Color(1, .894118, .882353),
                 'moccasin': Color(1, .894118, .709804),
                 'navajowhite': Color(1, .870588, .678431),
                 'navy': Color(0, 0, .501961),
                 'oldlace': Color(.992157, .960784, .901961),
                 'olive': Color(.501961, .501961, 0),
                 'olivedrab': Color(.419608, .556863, .137255),
                 'orange': Color(1, .647059, 0),
                 'orangered': Color(1, .270588, 0),
                 'orchid': Color(.854902, .439216, .839216),
                 'palegoldenrod': Color(.933333, .909804, .666667),
                 'palegreen': Color(.596078, .984314, .596078),
                 'paleturquoise': Color(.686275, .933333, .933333),
                 'palevioletred': Color(.858824, .439216, .576471),
                 'papayawhip': Color(1, .937255, .835294),
                 'peachpuff': Color(1, .854902, .72549),
                 'peru': Color(.803922, .521569, .247059),
                 'pink': Color(1, .752941, .796078),
                 'plum': Color(.866667, .627451, .866667),
                 'powderblue': Color(.690196, .878431, .901961),
                 'purple': Color(.501961, 0, .501961),
                 'red': Color(1, 0, 0),
                 'rosybrown': Color(.737255, .560784, .560784),
                 'royalblue': Color(.254902, .411765, .882353),
                 'saddlebrown': Color(.545098, .270588, .07451),
                 'salmon': Color(.980392, .501961, .447059),
                 'sandybrown': Color(.956863, .643137, .376471),
                 'scrollbar': Color(212, 208, 200),
                 'seagreen': Color(.180392, .545098, .341176),
                 'seashell': Color(1, .960784, .933333),
                 'sienna': Color(.627451, .321569, .176471),
                 'silver': Color(.752941, .752941, .752941),
                 'skyblue': Color(.529412, .807843, .921569),
                 'slateblue': Color(.415686, .352941, .803922),
                 'slategray': Color(.439216, .501961, .564706),
                 'slategrey': Color(.439216, .501961, .564706),
                 'snow': Color(1, .980392, .980392),
                 'springgreen': Color(0, 1, .498039),
                 'steelblue': Color(.27451, .509804, .705882),
                 'tan': Color(.823529, .705882, .54902),
                 'teal': Color(0, .501961, .501961),
                 'thistle': Color(.847059, .74902, .847059),
                 'threeddarkshadow': Color(64, 64, 64),
                 'threedface': Color(212, 208, 200),
                 'threedhighlight': Color(255, 255, 255),
                 'threedlightshadow': Color(212, 208, 200),
                 'threedshadow': Color(128, 128, 128),
                 'tomato': Color(1, .388235, .278431),
                 'turquoise': Color(.25098, .878431, .815686),
                 'violet': Color(.933333, .509804, .933333),
                 'wheat': Color(.960784, .870588, .701961),
                 'white': Color(1, 1, 1),
                 'whitesmoke': Color(.960784, .960784, .960784),
                 'window': Color(255, 255, 255),
                 'windowframe': Color(0, 0, 0),
                 'windowtext': Color(0, 0, 0),
                 'yellow': Color(1, 1, 0),
                 'yellowgreen': Color(.603922, .803922, .196078)}
