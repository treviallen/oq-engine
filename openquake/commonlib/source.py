# Copyright (c) 2010-2014, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import math
import copy
import logging
import operator
import collections
from itertools import izip
import random
from lxml import etree

from openquake.hazardlib import geo, mfd, pmf, source, gsim
from openquake.hazardlib.tom import PoissonTOM
from openquake.commonlib.node import read_nodes, context, striptag
from openquake.commonlib import valid, logictree
from openquake.commonlib.nrml import nodefactory, PARSE_NS_MAP
from openquake.commonlib import parallel

# this must stay here for the nrml_converters: don't remove it!
from openquake.commonlib.obsolete import NrmlHazardlibConverter

# the following is arbitrary, it is used to decide when to parallelize
# the filtering (MS)
LOTS_OF_SOURCES_SITES = 1E5

GSIMS = gsim.get_available_gsims()


class DuplicatedID(Exception):
    """Raised when two sources with the same ID are found in a source model"""


LtRealization = collections.namedtuple(
    'LtRealization', 'ordinal sm_lt_path gsim_lt_path weight')


SourceModel = collections.namedtuple(
    'SourceModel', 'name weight path trt_models gsim_lt ordinal')


class RlzAssoc(object):
    """
    Realization association class. It should not be instantiated directly,
    but only via the method :meth:
    `openquake.commonlib.source.CompositeSourceModel.get_rlz_assoc`.

    :attr realizations: list of LtRealization objects
    :attr gsim_by_trt: list of dictionaries {trt: gsim}
    :attr rlzs_assoc: dictionary {trt_model_id, gsim: rlzs}

    For instance, for the non-trivial logic tree in
    :mod:`openquake.qa_tests_data.classical.case_15`, which has 4 tectonic
    region types and 4 + 2 + 2 realizations, there are the following
    associations:

    (0, 'BooreAtkinson2008') ['#0-SM1-BA2008_C2003', '#1-SM1-BA2008_T2002']
    (0, 'CampbellBozorgnia2008') ['#2-SM1-CB2008_C2003', '#3-SM1-CB2008_T2002']
    (1, 'Campbell2003') ['#0-SM1-BA2008_C2003', '#2-SM1-CB2008_C2003']
    (1, 'ToroEtAl2002') ['#1-SM1-BA2008_T2002', '#3-SM1-CB2008_T2002']
    (2, 'BooreAtkinson2008') ['#4-SM2_a3pt2b0pt8-BA2008']
    (2, 'CampbellBozorgnia2008') ['#5-SM2_a3pt2b0pt8-CB2008']
    (3, 'BooreAtkinson2008') ['#6-SM2_a3b1-BA2008']
    (3, 'CampbellBozorgnia2008') ['#7-SM2_a3b1-CB2008']
    """
    def __init__(self):
        self.realizations = []
        self.gsim_by_trt = []  # [trt -> gsim]
        self.rlzs_assoc = collections.defaultdict(list)  # trt_id, gsim -> rlzs

    def _add_realizations(self, idx, lt_model, realizations):
        # create the realizations for the given lt source model
        trt_models = [tm for tm in lt_model.trt_models if tm.num_ruptures]
        if not trt_models:
            return idx
        gsims_by_trt = lt_model.gsim_lt.values
        for gsim_by_trt, weight, gsim_path, _ in realizations:
            if lt_model.weight is not None and weight is not None:
                weight = lt_model.weight * weight
            else:
                weight = None
            rlz = LtRealization(idx, lt_model.path, gsim_path, weight)
            self.realizations.append(rlz)
            self.gsim_by_trt.append(gsim_by_trt)
            for trt_model in trt_models:
                trt = trt_model.trt
                gsim = gsim_by_trt[trt]
                self.rlzs_assoc[trt_model.id, gsim].append(rlz)
                trt_model.gsims = gsims_by_trt[trt]
            idx += 1
        return idx

    def get_gsims_by_trt(self):
        """
        Return a dictionary trt_model_id -> [GSIM instances]
        """
        gsims_by_trt = collections.defaultdict(list)
        for trt_id, gsim in sorted(self.rlzs_assoc):
            gsims_by_trt[trt_id].append(GSIMS[gsim]())
        return gsims_by_trt

    def get_gsims_by(self, trt_model_id):
        """
        Return a dictionary trt_model_id -> [GSIM instances]
        """
        return [GSIMS[gsim]()
                for trt_id, gsim in sorted(self.rlzs_assoc)
                if trt_id == trt_model_id]


class TrtModel(collections.Sequence):
    """
    A container for the following parameters:

    :param str trt:
        the tectonic region type all the sources belong to
    :param list sources:
        a list of hazardlib source objects
    :param int num_ruptures:
        the total number of ruptures generated by the given sources
    :param min_mag:
        the minimum magnitude among the given sources
    :param max_mag:
        the maximum magnitude among the given sources
    :param gsims:
        the GSIMs associated to tectonic region type
    :param id:
        an optional numeric ID (default None) useful to associate
        the model to a database object
    """
    POINT_SOURCE_WEIGHT = 1 / 40.

    def __init__(self, trt, sources=None, num_ruptures=0,
                 min_mag=None, max_mag=None, gsims=None, id=0):
        self.trt = trt
        self.sources = sources or []
        self.num_ruptures = num_ruptures
        self.min_mag = min_mag
        self.max_mag = max_mag
        self.gsims = gsims or []
        self.id = id
        for src in self.sources:
            self.update(src)

    def update(self, src):
        """
        Update the attributes sources, min_mag, max_mag
        according to the given source.

        :param src:
            an instance of :class:
            `openquake.hazardlib.source.base.BaseSeismicSource`
        """
        assert src.tectonic_region_type == self.trt, (
            src.tectonic_region_type, self.trt)
        self.sources.append(src)
        min_mag, max_mag = src.get_min_max_mag()
        prev_min_mag = self.min_mag
        if prev_min_mag is None or min_mag < prev_min_mag:
            self.min_mag = min_mag
        prev_max_mag = self.max_mag
        if prev_max_mag is None or max_mag > prev_max_mag:
            self.max_mag = max_mag

    def update_num_ruptures(self, src):
        """
        Update the attribute num_ruptures according to the given source.

        :param src:
            an instance of :class:
            `openquake.hazardlib.source.base.BaseSeismicSource`
        :returns:
            the weight of the source, as a function of the number
            of ruptures generated by the source
        """
        num_ruptures = src.count_ruptures()
        self.num_ruptures += num_ruptures
        weight = (num_ruptures * self.POINT_SOURCE_WEIGHT
                  if src.__class__.__name__ == 'PointSource'
                  else num_ruptures)
        return weight

    def split_sources_and_count_ruptures(self, area_source_discretization):
        """
        Split the current .sources and replace them with new ones.
        Also, update the total .num_ruptures and the .weigth of each
        source. Finally, make sure the sources are ordered.

        :param area_source_discretization: parameter from the job.ini
        """
        sources = []
        for src in self:
            for ss in split_source(src, area_source_discretization):
                ss.weight = self.update_num_ruptures(ss)
                sources.append(ss)
        self.sources = sorted(sources, key=operator.attrgetter('source_id'))

    def __repr__(self):
        return '<%s #%d %s, %d source(s)>' % (
            self.__class__.__name__, self.id, self.trt, len(self.sources))

    def __lt__(self, other):
        """
        Make sure there is a precise ordering of TrtModel objects.
        Objects with less sources are put first; in case the number
        of sources is the same, use lexicographic ordering on the trts
        """
        num_sources = len(self.sources)
        other_sources = len(other.sources)
        if num_sources == other_sources:
            return self.trt < other.trt
        return num_sources < other_sources

    def __getitem__(self, i):
        return self.sources[i]

    def __iter__(self):
        return iter(self.sources)

    def __len__(self):
        return len(self.sources)


def parse_source_model(fname, converter, apply_uncertainties=lambda src: None):
    """
    Parse a NRML source model and return an ordered list of TrtModel
    instances.

    :param str fname:
        the full pathname of the source model file
    :param converter:
        :class:`openquake.commonlib.source.SourceConverter` instance
    :param apply_uncertainties:
        a function modifying the sources (or do nothing)
    """
    converter.fname = fname
    source_stats_dict = {}
    source_ids = set()
    src_nodes = read_nodes(fname, lambda elem: 'Source' in elem.tag,
                           nodefactory['sourceModel'])
    for no, src_node in enumerate(src_nodes, 1):
        src = converter.convert_node(src_node)
        if src.source_id in source_ids:
            raise DuplicatedID(
                'The source ID %s is duplicated!' % src.source_id)
        apply_uncertainties(src)
        trt = src.tectonic_region_type
        if trt not in source_stats_dict:
            source_stats_dict[trt] = TrtModel(trt)
        source_stats_dict[trt].update(src)
        source_ids.add(src.source_id)
        if no % 10000 == 0:  # log every 10,000 sources parsed
            logging.info('Parsed %d sources from %s', no, fname)

    # return ordered TrtModels
    return sorted(source_stats_dict.itervalues())


def area_to_point_sources(area_src, area_src_disc):
    """
    Split an area source into a generator of point sources.

    MFDs will be rescaled appropriately for the number of points in the area
    mesh.

    :param area_src:
        :class:`openquake.hazardlib.source.AreaSource`
    :param float area_src_disc:
        Area source discretization step, in kilometers.
    """
    mesh = area_src.polygon.discretize(area_src_disc)
    num_points = len(mesh)
    area_mfd = area_src.mfd

    if isinstance(area_mfd, mfd.TruncatedGRMFD):
        new_a_val = math.log10(10 ** area_mfd.a_val / float(num_points))
        new_mfd = mfd.TruncatedGRMFD(
            a_val=new_a_val,
            b_val=area_mfd.b_val,
            bin_width=area_mfd.bin_width,
            min_mag=area_mfd.min_mag,
            max_mag=area_mfd.max_mag)
    elif isinstance(area_mfd, mfd.EvenlyDiscretizedMFD):
        new_occur_rates = [float(x) / num_points
                           for x in area_mfd.occurrence_rates]
        new_mfd = mfd.EvenlyDiscretizedMFD(
            min_mag=area_mfd.min_mag,
            bin_width=area_mfd.bin_width,
            occurrence_rates=new_occur_rates)

    for i, (lon, lat) in enumerate(izip(mesh.lons, mesh.lats)):
        pt = source.PointSource(
            # Generate a new ID and name
            source_id='%s-%s' % (area_src.source_id, i),
            name='%s-%s' % (area_src.name, i),
            tectonic_region_type=area_src.tectonic_region_type,
            mfd=new_mfd,
            rupture_mesh_spacing=area_src.rupture_mesh_spacing,
            magnitude_scaling_relationship=
            area_src.magnitude_scaling_relationship,
            rupture_aspect_ratio=area_src.rupture_aspect_ratio,
            upper_seismogenic_depth=area_src.upper_seismogenic_depth,
            lower_seismogenic_depth=area_src.lower_seismogenic_depth,
            location=geo.Point(lon, lat),
            nodal_plane_distribution=area_src.nodal_plane_distribution,
            hypocenter_distribution=area_src.hypocenter_distribution,
            temporal_occurrence_model=area_src.temporal_occurrence_model)
        yield pt


def split_fault_source(src):
    """
    Generator splitting a fault source into several fault sources,
    one for each magnitude.

    :param src:
        an instance of :class:`openquake.hazardlib.source.base.SeismicSource`
    """
    i = 0  # split source index
    for mag, rate in src.mfd.get_annual_occurrence_rates():
        if rate:  # ignore zero occurency rate
            new_src = copy.copy(src)
            new_src.source_id = '%s-%s' % (src.source_id, i)
            new_src.mfd = mfd.EvenlyDiscretizedMFD(
                min_mag=mag, bin_width=src.mfd.bin_width,
                occurrence_rates=[rate])
            i += 1
            yield new_src


def split_source(src, area_source_discretization):
    """
    Split an area source into point sources and a fault sources into
    smaller fault sources.

    :param src:
        an instance of :class:`openquake.hazardlib.source.base.SeismicSource`
    :param float area_source_discretization:
        area source discretization
    """
    if isinstance(src, source.AreaSource):
        for s in area_to_point_sources(src, area_source_discretization):
            yield s
    elif isinstance(
            src, (source.SimpleFaultSource, source.ComplexFaultSource)):
        for s in split_fault_source(src):
            yield s
    else:  # characteristic sources are not split since they are small
        yield src


def split_coords_2d(seq):
    """
    :param seq: a flat list with lons and lats
    :returns: a validated list of pairs (lon, lat)

    >>> split_coords_2d([1.1, 2.1, 2.2, 2.3])
    [(1.1, 2.1), (2.2, 2.3)]
    """
    lons, lats = [], []
    for i, el in enumerate(seq):
        if i % 2 == 0:
            lons.append(valid.longitude(el))
        elif i % 2 == 1:
            lats.append(valid.latitude(el))
    return zip(lons, lats)


def split_coords_3d(seq):
    """
    :param seq: a flat list with lons, lats and depths
    :returns: a validated list of (lon, lat, depths) triplets

    >>> split_coords_3d([1.1, 2.1, 0.1, 2.3, 2.4, 0.1])
    [(1.1, 2.1, 0.1), (2.3, 2.4, 0.1)]
    """
    lons, lats, depths = [], [], []
    for i, el in enumerate(seq):
        if i % 3 == 0:
            lons.append(valid.longitude(el))
        elif i % 3 == 1:
            lats.append(valid.latitude(el))
        elif i % 3 == 2:
            depths.append(valid.depth(el))
    return zip(lons, lats, depths)


class RuptureConverter(object):
    """
    Convert ruptures from nodes into Hazardlib ruptures.
    """
    fname = None  # should be set externally

    def __init__(self, rupture_mesh_spacing, complex_fault_mesh_spacing):
        self.rupture_mesh_spacing = rupture_mesh_spacing
        self.complex_fault_mesh_spacing = complex_fault_mesh_spacing

    def convert_node(self, node):
        """
        Convert the given rupture node into a hazardlib rupture, depending
        on the node tag.

        :param node: a node representing a rupture
        """
        with context(self.fname, node):
            convert_rupture = getattr(self, 'convert_' + striptag(node.tag))
            mag = ~node.magnitude
            rake = ~node.rake
            hypocenter = ~node.hypocenter
        return convert_rupture(node, mag, rake, hypocenter)

    def geo_line(self, edge):
        """
        Utility function to convert a node of kind edge
        into a :class:`openquake.hazardlib.geo.Line` instance.

        :param edge: a node describing an edge
        """
        with context(self.fname, edge.LineString.posList) as plist:
            coords = split_coords_2d(~plist)
        return geo.Line([geo.Point(*p) for p in coords])

    def geo_lines(self, edges):
        """
        Utility function to convert a list of edges into a list of
        :class:`openquake.hazardlib.geo.Line` instances.

        :param edge: a node describing an edge
        """
        lines = []
        for edge in edges:
            with context(self.fname, edge):
                coords = split_coords_3d(~edge.LineString.posList)
            lines.append(geo.Line([geo.Point(*p) for p in coords]))
        return lines

    def geo_planar(self, surface):
        """
        Utility to convert a PlanarSurface node with subnodes
        topLeft, topRight, bottomLeft, bottomRight into a
        :class:`openquake.hazardlib.geo.PlanarSurface` instance.

        :param surface: PlanarSurface node
        """
        with context(self.fname, surface):
            top_left = geo.Point(*~surface.topLeft)
            top_right = geo.Point(*~surface.topRight)
            bottom_left = geo.Point(*~surface.bottomLeft)
            bottom_right = geo.Point(*~surface.bottomRight)
        return geo.PlanarSurface.from_corner_points(
            self.rupture_mesh_spacing,
            top_left, top_right, bottom_right, bottom_left)

    def convert_surfaces(self, surface_nodes):
        """
        Utility to convert a list of surface nodes into a single hazardlib
        surface. There are three possibilities:

        1. there is a single simpleFaultGeometry node; returns a
           :class:`openquake.hazardlib.geo.simpleFaultSurface` instance
        2. there is a single complexFaultGeometry node; returns a
           :class:`openquake.hazardlib.geo.complexFaultSurface` instance
        3. there is a list of PlanarSurface nodes; returns a
           :class:`openquake.hazardlib.geo.MultiSurface` instance

        :param surface_nodes: surface nodes as just described
        """
        surface_node = surface_nodes[0]
        if surface_node.tag.endswith('simpleFaultGeometry'):
            surface = geo.SimpleFaultSurface.from_fault_data(
                self.geo_line(surface_node),
                ~surface_node.upperSeismoDepth,
                ~surface_node.lowerSeismoDepth,
                ~surface_node.dip,
                self.rupture_mesh_spacing)
        elif surface_node.tag.endswith('complexFaultGeometry'):
            surface = geo.ComplexFaultSurface.from_fault_data(
                self.geo_lines(surface_node),
                self.complex_fault_mesh_spacing)
        else:  # a collection of planar surfaces
            planar_surfaces = map(self.geo_planar, surface_nodes)
            surface = geo.MultiSurface(planar_surfaces)
        return surface

    def convert_simpleFaultRupture(self, node, mag, rake, hypocenter):
        """
        Convert a simpleFaultRupture node.

        :param node: the rupture node
        :param mag: the rupture magnitude
        :param rake: the rupture rake angle
        :param hypocenter: the rupture hypocenter
        """
        with context(self.fname, node):
            surfaces = [node.simpleFaultGeometry]
        rupt = source.rupture.Rupture(
            mag=mag, rake=rake, tectonic_region_type=None,
            hypocenter=geo.Point(*hypocenter),
            surface=self.convert_surfaces(surfaces),
            source_typology=source.SimpleFaultSource)
        return rupt

    def convert_complexFaultRupture(self, node, mag, rake, hypocenter):
        """
        Convert a complexFaultRupture node.

        :param node: the rupture node
        :param mag: the rupture magnitude
        :param rake: the rupture rake angle
        :param hypocenter: the rupture hypocenter
        """
        with context(self.fname, node):
            surfaces = [node.complexFaultGeometry]
        rupt = source.rupture.Rupture(
            mag=mag, rake=rake, tectonic_region_type=None,
            hypocenter=geo.Point(*hypocenter),
            surface=self.convert_surfaces(surfaces),
            source_typology=source.ComplexFaultSource)
        return rupt

    def convert_singlePlaneRupture(self, node, mag, rake, hypocenter):
        """
        Convert a singlePlaneRupture node.

        :param node: the rupture node
        :param mag: the rupture magnitude
        :param rake: the rupture rake angle
        :param hypocenter: the rupture hypocenter
        """
        with context(self.fname, node):
            surfaces = [node.planarSurface]
        hrupt = source.rupture.Rupture(
            mag=mag, rake=rake,
            tectonic_region_type=None,
            hypocenter=geo.Point(*hypocenter),
            surface=self.convert_surfaces(surfaces),
            source_typology=source.NonParametricSeismicSource)
        return hrupt

    def convert_multiPlanesRupture(self, node, mag, rake, hypocenter):
        """
        Convert a multiPlanesRupture node.

        :param node: the rupture node
        :param mag: the rupture magnitude
        :param rake: the rupture rake angle
        :param hypocenter: the rupture hypocenter
        """
        with context(self.fname, node):
            surfaces = list(node.getnodes('planarSurface'))
        hrupt = source.rupture.Rupture(
            mag=mag, rake=rake,
            tectonic_region_type=None,
            hypocenter=geo.Point(*hypocenter),
            surface=self.convert_surfaces(surfaces),
            source_typology=source.NonParametricSeismicSource)
        return hrupt


class SourceConverter(RuptureConverter):
    """
    Convert sources from valid nodes into Hazardlib objects.
    """
    def __init__(self, investigation_time, rupture_mesh_spacing,
                 complex_fault_mesh_spacing, width_of_mfd_bin,
                 area_source_discretization):
        self.area_source_discretization = area_source_discretization
        self.rupture_mesh_spacing = rupture_mesh_spacing
        self.complex_fault_mesh_spacing = complex_fault_mesh_spacing
        self.width_of_mfd_bin = width_of_mfd_bin
        self.tom = PoissonTOM(investigation_time)

    def convert_node(self, node):
        """
        Convert the given node into a hazardlib source, depending
        on the node tag.

        :param node: a node representing a source
        """
        with context(self.fname, node):
            convert_source = getattr(self, 'convert_' + striptag(node.tag))
        return convert_source(node)

    def convert_mfdist(self, node):
        """
        Convert the given node into a Magnitude-Frequency Distribution
        object.

        :param node: a node of kind incrementalMFD or truncGutenbergRichterMFD
        :returns: a :class:`openquake.hazardlib.mdf.EvenlyDiscretizedMFD.` or
                  :class:`openquake.hazardlib.mdf.TruncatedGRMFD` instance
        """
        with context(self.fname, node):
            [mfd_node] = [subnode for subnode in node
                          if subnode.tag.endswith(
                              ('incrementalMFD', 'truncGutenbergRichterMFD'))]
            if mfd_node.tag.endswith('incrementalMFD'):
                return mfd.EvenlyDiscretizedMFD(
                    min_mag=mfd_node['minMag'], bin_width=mfd_node['binWidth'],
                    occurrence_rates=~mfd_node.occurRates)
            elif mfd_node.tag.endswith('truncGutenbergRichterMFD'):
                return mfd.TruncatedGRMFD(
                    a_val=mfd_node['aValue'], b_val=mfd_node['bValue'],
                    min_mag=mfd_node['minMag'], max_mag=mfd_node['maxMag'],
                    bin_width=self.width_of_mfd_bin)

    def convert_npdist(self, node):
        """
        Convert the given node into a Nodal Plane Distribution.

        :param node: a nodalPlaneDist node
        :returns: a :class:`openquake.hazardlib.geo.NodalPlane` instance
        """
        with context(self.fname, node):
            npdist = []
            for np in node.nodalPlaneDist:
                prob, strike, dip, rake = ~np
                npdist.append((prob, geo.NodalPlane(strike, dip, rake)))
            return pmf.PMF(npdist)

    def convert_hpdist(self, node):
        """
        Convert the given node into a probability mass function for the
        hypo depth distribution.

        :param node: a hypoDepthDist node
        :returns: a :class:`openquake.hazardlib.pmf.PMF` instance
        """
        with context(self.fname, node):
            return pmf.PMF([~hd for hd in node.hypoDepthDist])

    def convert_areaSource(self, node):
        """
        Convert the given node into an area source object.

        :param node: a node with tag areaGeometry
        :returns: a :class:`openquake.hazardlib.source.AreaSource` instance
        """
        geom = node.areaGeometry
        coords = split_coords_2d(~geom.Polygon.exterior.LinearRing.posList)
        polygon = geo.Polygon([geo.Point(*xy) for xy in coords])
        msr = valid.SCALEREL[~node.magScaleRel]()
        return source.AreaSource(
            source_id=node['id'],
            name=node['name'],
            tectonic_region_type=node['tectonicRegion'],
            mfd=self.convert_mfdist(node),
            rupture_mesh_spacing=self.rupture_mesh_spacing,
            magnitude_scaling_relationship=msr,
            rupture_aspect_ratio=~node.ruptAspectRatio,
            upper_seismogenic_depth=~geom.upperSeismoDepth,
            lower_seismogenic_depth=~geom.lowerSeismoDepth,
            nodal_plane_distribution=self.convert_npdist(node),
            hypocenter_distribution=self.convert_hpdist(node),
            polygon=polygon,
            area_discretization=self.area_source_discretization,
            temporal_occurrence_model=self.tom)

    def convert_pointSource(self, node):
        """
        Convert the given node into a point source object.

        :param node: a node with tag pointGeometry
        :returns: a :class:`openquake.hazardlib.source.PointSource` instance
        """
        geom = node.pointGeometry
        lon_lat = ~geom.Point.pos
        msr = valid.SCALEREL[~node.magScaleRel]()
        return source.PointSource(
            source_id=node['id'],
            name=node['name'],
            tectonic_region_type=node['tectonicRegion'],
            mfd=self.convert_mfdist(node),
            rupture_mesh_spacing=self.rupture_mesh_spacing,
            magnitude_scaling_relationship=msr,
            rupture_aspect_ratio=~node.ruptAspectRatio,
            upper_seismogenic_depth=~geom.upperSeismoDepth,
            lower_seismogenic_depth=~geom.lowerSeismoDepth,
            location=geo.Point(*lon_lat),
            nodal_plane_distribution=self.convert_npdist(node),
            hypocenter_distribution=self.convert_hpdist(node),
            temporal_occurrence_model=self.tom)

    def convert_simpleFaultSource(self, node):
        """
        Convert the given node into a simple fault object.

        :param node: a node with tag areaGeometry
        :returns: a :class:`openquake.hazardlib.source.SimpleFaultSource`
                  instance
        """
        geom = node.simpleFaultGeometry
        msr = valid.SCALEREL[~node.magScaleRel]()
        simple = source.SimpleFaultSource(
            source_id=node['id'],
            name=node['name'],
            tectonic_region_type=node['tectonicRegion'],
            mfd=self.convert_mfdist(node),
            rupture_mesh_spacing=self.rupture_mesh_spacing,
            magnitude_scaling_relationship=msr,
            rupture_aspect_ratio=~node.ruptAspectRatio,
            upper_seismogenic_depth=~geom.upperSeismoDepth,
            lower_seismogenic_depth=~geom.lowerSeismoDepth,
            fault_trace=self.geo_line(geom),
            dip=~geom.dip,
            rake=~node.rake,
            temporal_occurrence_model=self.tom)
        return simple

    def convert_complexFaultSource(self, node):
        """
        Convert the given node into a complex fault object.

        :param node: a node with tag areaGeometry
        :returns: a :class:`openquake.hazardlib.source.ComplexFaultSource`
                  instance
        """
        geom = node.complexFaultGeometry
        msr = valid.SCALEREL[~node.magScaleRel]()
        cmplx = source.ComplexFaultSource(
            source_id=node['id'],
            name=node['name'],
            tectonic_region_type=node['tectonicRegion'],
            mfd=self.convert_mfdist(node),
            rupture_mesh_spacing=self.complex_fault_mesh_spacing,
            magnitude_scaling_relationship=msr,
            rupture_aspect_ratio=~node.ruptAspectRatio,
            edges=self.geo_lines(geom),
            rake=~node.rake,
            temporal_occurrence_model=self.tom)
        return cmplx

    def convert_characteristicFaultSource(self, node):
        """
        Convert the given node into a characteristic fault object.

        :param node:
            a node with tag areaGeometry
        :returns:
            a :class:`openquake.hazardlib.source.CharacteristicFaultSource`
            instance
        """
        char = source.CharacteristicFaultSource(
            source_id=node['id'],
            name=node['name'],
            tectonic_region_type=node['tectonicRegion'],
            mfd=self.convert_mfdist(node),
            surface=self.convert_surfaces(node.surface),
            rake=~node.rake,
            temporal_occurrence_model=self.tom)
        return char

    def convert_nonParametricSeismicSource(self, node):
        """
        Convert the given node into a non parametric source object.

        :param node:
            a node with tag areaGeometry
        :returns:
            a :class:`openquake.hazardlib.source.NonParametricSeismicSource`
            instance
        """
        trt = node['tectonicRegion']
        rup_pmf_data = []
        for rupnode in node:
            probs = pmf.PMF(rupnode['probs_occur'])
            rup = RuptureConverter.convert_node(self, rupnode)
            rup.tectonic_region_type = trt
            rup_pmf_data.append((rup, probs))
        nps = source.NonParametricSeismicSource(
            node['id'], node['name'], trt, rup_pmf_data)
        return nps


def parse_ses_ruptures(fname):
    """
    Convert a stochasticEventSetCollection file into a set of SES,
    each one containing ruptures with a tag and a seed.
    """
    raise NotImplementedError('parse_ses_ruptures')


def _filter_sources(sources, sitecol, maxdist):
    # called by filter_sources
    srcs = []
    for src in sources:
        sites = src.filter_sites_by_distance_to_source(maxdist, sitecol)
        if sites is not None:
            srcs.append(src)
    return srcs


def filter_sources(sources, sitecol, maxdist):
    """
    Filter a list of hazardlib sources according to the maximum distance.

    :param sources: the original sources
    :param sitecol: a :class:`openquake.hazardlib.site.SiteCollection` instance
    :param maxdist: maximum distance
    :returns: the filtered sources ordered by source_id
    """
    if len(sources) * len(sitecol) > LOTS_OF_SOURCES_SITES:
        # filter in parallel on all available cores
        sources = parallel.apply_reduce(
            _filter_sources, (sources, sitecol, maxdist), operator.add, [])
    else:
        # few sources and sites, filter sequentially on a single core
        sources = _filter_sources(sources, sitecol, maxdist)
    return sorted(sources, key=operator.attrgetter('source_id'))


class CompositeSourceModel(collections.Sequence):
    """
    :param source_model_lt:
        a :class:`openquake.commonlib.readinput.SourceModelLogicTree` instance
    :param source_models:
        a list of :class:`openquake.commonlib.readinput.SourceModel` tuples
    """
    def __init__(self, source_model_lt, source_models):
        self.source_model_lt = source_model_lt
        self.source_models = list(source_models)
        if not self.source_models:
            raise RuntimeError('All sources were filtered away')
        self.tmdict = {}
        for i, tm in enumerate(self.trt_models):
            tm.id = i
            self.tmdict[i] = tm

    @property
    def trt_models(self):
        """
        Yields the TrtModels inside each source model in order
        """
        for sm in self.source_models:
            for trt_model in sm.trt_models:
                yield trt_model

    @property
    def sources(self):
        """
        Yield the sources contained in the internal source models in order
        """
        for trt_model in self.trt_models:
            for src in trt_model:
                src.trt_model_id = trt_model.id
                yield src

    def reduce_trt_models(self):
        """
        Remove the tectonic regions without ruptures and reduce the
        GSIM logic tree. It works by updating the underlying source models.
        """
        for sm in self:
            trts = set(trt_model.trt for trt_model in sm.trt_models
                       if trt_model.num_ruptures > 0)
            if trts == set(sm.gsim_lt.filter_keys):
                # nothing to remove
                continue
            # build the reduced logic tree
            gsim_lt = sm.gsim_lt.filter(trts)
            tmodels = []  # collect the reduced trt models
            for trt_model in sm.trt_models:
                if trt_model.trt in trts:
                    trt_model.gsims = gsim_lt.values[trt_model.trt]
                    tmodels.append(trt_model)
            self[sm.ordinal] = SourceModel(
                sm.name, sm.weight, sm.path, tmodels, gsim_lt, sm.ordinal)

    def get_source_model(self, path):
        """
        Extract a specific source model, given its logic tree path.

        :param: the source model logic tree path as a tuple of string
        """
        for sm in self:
            if sm.path == path:
                return sm
        raise KeyError(
            'There is no source model with sm_lt_path=%s' % str(path))

    def get_rlz_assoc(self):
        """
        Return a RlzAssoc with fields realizations, gsim_by_trt,
        rlz_idx and trt_gsims.
        """
        assoc = RlzAssoc()
        random_seed = self.source_model_lt.seed
        num_samples = self.source_model_lt.num_samples
        idx = 0
        for sm_name, weight, sm_lt_path, _ in self.source_model_lt:
            lt_model = self.get_source_model(sm_lt_path)
            if num_samples:  # sampling, pick just one gsim realization
                rnd = random.Random(random_seed + idx)
                rlzs = [logictree.sample_one(lt_model.gsim_lt, rnd)]
            else:
                rlzs = list(lt_model.gsim_lt)  # full enumeration
            logging.info('Creating %d GMPE realization(s) for model %s, %s',
                         len(rlzs), lt_model.name, lt_model.path)
            idx = assoc._add_realizations(idx, lt_model, rlzs)

        num_ind_rlzs = sum(sm.gsim_lt.get_num_paths() for sm in self)
        if num_samples > num_ind_rlzs:
            logging.warn("""
The number of independent realizations is %d but you are using %d samplings.
That means that some GMPEs will be sampled more than once, resulting in
duplicated data and redundant computation. You should switch to full
enumeration mode, i.e. set number_of_logic_tree_samples=0 in your .ini file.
""", num_ind_rlzs, num_samples)
        return assoc

    def __getitem__(self, i):
        """Return the i-th source model"""
        return self.source_models[i]

    def __setitem__(self, i, sm):
        """Update the i-th source model"""
        self.source_models[i] = sm

    def __iter__(self):
        """Return an iterator over the underlying source models"""
        return iter(self.source_models)

    def __len__(self):
        """Return the number of underlying source models"""
        return len(self.source_models)


def _collect_source_model_paths(smlt):
    """
    Given a path to a source model logic tree or a file-like, collect all of
    the soft-linked path names to the source models it contains and return them
    as a uniquified list (no duplicates).
    """
    src_paths = []
    tree = etree.parse(smlt)
    for branch_set in tree.xpath('//nrml:logicTreeBranchSet',
                                 namespaces=PARSE_NS_MAP):

        if branch_set.get('uncertaintyType') == 'sourceModel':
            for branch in branch_set.xpath(
                    './nrml:logicTreeBranch/nrml:uncertaintyModel',
                    namespaces=PARSE_NS_MAP):
                src_paths.append(branch.text)
    return sorted(set(src_paths))
