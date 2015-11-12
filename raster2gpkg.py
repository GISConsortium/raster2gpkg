﻿#!/usr/bin/env python
"""Simple script to add a dataset into an existing or new GeoPackage.
Based on other python utilities but made PEP compliant.
"""
# $Id$

import arcpy
import os
import sys
import re
import math
import sqlite3
import numpy
import tempfile
import collections
from osgeo import gdal
from osgeo.gdalconst import *
import cache2gpkg
import shutil


TILES_TABLE_SUFFIX = '_tiles'             # Added to basename to create table_name
TILES_TABLE_PREFIX = 'table_'             # Used if basename starts with a non alphabet character

class GeoPackage:
    """
    Simple class to add tiles to an existing or new GeoPackage using GDAL.
    """

    def __init__(self, fmt='image/jpeg'):
        self.connection = None
        self.filename = None
        self.tile_width = 256
        self.tile_height = 256
        self.full_width = 0
        self.full_height = 0
        self.format = fmt
        self.format_options = list()
        self.sr_organization = "NONE"
        self.sr_organization_coordsys_id = 0
        self.sr_description = None
        self.data_type = GDT_Unknown
        self.mem_driver = gdal.GetDriverByName("MEM")
        if self.format == 'image/jpeg':
            self.driver = gdal.GetDriverByName('JPEG')
        elif self.format == 'image/png':
            self.driver = gdal.GetDriverByName("PNG")
        if self.driver is None or self.mem_driver is None:
            raise RuntimeError("Can't find appropriate GDAL driver for MEM and/or format ", self.format)
        self.jpeg_options = []
        self.ResolutionLayerInfo = collections.namedtuple('ResolutionLayerInfo', ['factor_x', 'factor_y',
                                                                                  'pixel_x_size', 'pixel_y_size',
                                                                                  'width', 'height',
                                                                                  'matrix_width', 'matrix_height',
                                                                                  'expected_pixel_x_size',
                                                                                  'expected_pixel_y_size'])
        self.overviews = []
        self.tile_lod_info = [
            [156543.033928, 591657527.591555],
            [78271.5169639999, 295828763.795777],
            [39135.7584820001, 147914381.897889],
            [19567.8792409999, 73957190.948944],
            [9783.93962049996, 36978595.474472],
            [4891.96981024998, 18489297.737236],
            [2445.98490512499, 9244648.868618],
            [1222.99245256249, 4622324.434309],
            [611.49622628138, 2311162.217155],
            [305.748113140558, 1155581.108577],
            [152.874056570411, 577790.554289],
            [76.4370282850732, 288895.277144],
            [38.2185141425366, 144447.638572],
            [19.1092570712683, 72223.819286],
            [9.55462853563415, 36111.909643],
            [4.77731426794937, 18055.954822],
            [2.38865713397468, 9027.977411],
            [1.19432856685505, 4513.988705],
            [0.597164283559817, 2256.994353],
            [0.298582141647617, 1128.497176],
            ]

    def __del__(self):
        if self.connection is not None:
            self.connection.close()

    def compute_levels(self, pixel_x_size, pixel_y_size, width, height, map_width, map_height):
        level = 1
        self.full_width = width
        self.full_height = height
        width = int(math.pow(2, math.floor(math.log(width)/math.log(2))))
        height = int(math.pow(2, math.floor(math.log(height)/math.log(2))))
        matrix_width = int(math.ceil(float(width) / float(self.tile_width)))
        matrix_height = int(math.ceil(float(height) / float(self.tile_height)))
        layer_pixel_x_size = (map_width / matrix_width) / self.tile_width
        layer_pixel_y_size = (map_height / matrix_height) / self.tile_height
        layer_width = int(math.ceil(map_width / layer_pixel_x_size))
        layer_height = int(math.ceil(map_height / layer_pixel_y_size))
        factor_x = float(self.full_width) / float(layer_width)
        factor_y = float(self.full_height) / float(layer_height)
        expected_pixel_x_size = (map_width / matrix_width) / self.tile_width
        expected_pixel_y_size = (map_height / matrix_height) / self.tile_height
        self.overviews.append(self.ResolutionLayerInfo(factor_x, factor_y, layer_pixel_x_size, layer_pixel_y_size,
                                                       layer_width, layer_height, matrix_width, matrix_height,
                                                       expected_pixel_x_size, expected_pixel_y_size))
        while 1:
            factor = pow(2, level)
            overview_pixel_x_size = layer_pixel_x_size * float(factor)
            overview_pixel_y_size = layer_pixel_y_size * float(factor)
            overview_width = int(math.ceil(map_width / overview_pixel_x_size))
            overview_height = int(math.ceil(map_height / overview_pixel_y_size))
            matrix_width = int(math.floor(float(overview_width) / float(self.tile_width)))
            matrix_height = int(math.floor(float(overview_height) / float(self.tile_height)))
            expected_pixel_x_size = (map_width / matrix_width) / self.tile_width
            expected_pixel_y_size = (map_height / matrix_height) / self.tile_height
            factor_x = float(self.full_width) / float(overview_width)
            factor_y = float(self.full_height) / float(overview_height)
            if overview_width < 1024 or overview_height < 1024:
                break
            self.overviews.append(self.ResolutionLayerInfo(factor_x, factor_y, overview_pixel_x_size, overview_pixel_y_size,
                                                           overview_width, overview_height,
                                                           matrix_width, matrix_height,
                                                           expected_pixel_x_size, expected_pixel_y_size))
            level += 1
        # for overview in self.overviews:
        #     print(overview.factor_x, overview.factor_y, overview.pixel_x_size, overview.pixel_y_size, overview.width, overview.height,
        #           overview.matrix_width, overview.matrix_height, overview.expected_pixel_x_size, overview.expected_pixel_y_size)
        #     print((map_width/overview.matrix_width)/self.tile_width,(map_height/overview.matrix_height)/self.tile_height)
        return True


    def write_srs(self, dataset, srs_name):
        """
        Extract and write SRS to gpkg_spatial_ref_sys table and return srs_id.
        @param dataset: Input dataset.
        @param srs_name: Value for srs_name field.
        @return: srs_id for new entry or -1 (undefined cartesian)
        """
        if dataset is None:
            return -1
        result = self.connection.execute("""SELECT * FROM gpkg_spatial_ref_sys WHERE srs_name=?;""",
                                         (srs_name,)).fetchone()
        if result is None:
            prj = dataset.GetProjectionRef()
            if prj is not None:
                result = self.connection.execute(
                    """SELECT MAX(srs_id) AS max_id FROM gpkg_spatial_ref_sys;""").fetchone()
                if result is None:
                    return -1
                srs_id = result['max_id']
                srs_id += 1
                self.connection.execute(
                    """
                    INSERT INTO gpkg_spatial_ref_sys(srs_name, srs_id, organization, organization_coordsys_id, definition)
                                VALUES(?, ?, 'NONE', 0, ?)
                    """, (srs_name, srs_id, prj))
                self.connection.commit()
                return srs_id
        else:
            return result['srs_id']

    def add_dataset(self, filename):
        dataset = gdal.Open(filename, GA_ReadOnly)
        if dataset is None:
            return False
        if dataset.RasterCount < 1:
            print(filename, " does not contain any bands.")
            return False
        #if dataset.RasterCount == 1 and dataset.GetRasterBand(1).GetRasterColorInterpretation() == GCI_PaletteIndex:
        #    print("Colormap is not supported.")
        #    return False
        self.data_type = dataset.GetRasterBand(1).DataType
        #if self.format == 'image/jpeg':
        #    if dataset.RasterCount != 1 and dataset.RasterCount != 3:
        #        print("For image/jpeg, only datasets with 1 (grayscale) or 3 (RGB/YCbCr) bands are allowed.")
        #        return False
        #    if self.data_type != GDT_Byte:
        #        print("For image/jpeg, only 8 bit unsigned data is supported.")
        #        return False
        #elif self.format == 'image/png':
        #    if dataset.RasterCount > 4:
        #        print("For image/png, only datasets with 1 (grayscale), 2 (grascale + alpha), "
        #              "3 (RGB) or 4 (RGBA) bands are allowed.")
        #        return False
        #    if self.data_type != GDT_Byte and self.data_type != GDT_UInt16:
        #        print("For image/png, only 8 or 16 bit unsigned data is supported.")
        #        return False
        #else:
        #    print("Unsupported format specified: ", self.format)
        #    return False

        tempFolder = tempfile.mkdtemp(suffix='_gpkg_cache')
        cacheName = os.path.basename(filename)

        pixel_x_size = 0.0
        pixel_y_size = 0.0

        min_x = 0.0
        min_y = 0.0
        max_x = 0.0
        max_y = 0.0

        geotransform = dataset.GetGeoTransform(can_return_null=True)
        if geotransform is not None:
            pixel_x_size = geotransform[1]
            pixel_y_size = abs(geotransform[5])
            min_x = geotransform[0]
            min_y = geotransform[3]
            max_x = geotransform[0] + geotransform[1] * dataset.RasterXSize + geotransform[2] * dataset.RasterYSize
            max_y = geotransform[3] + geotransform[4] * dataset.RasterXSize + geotransform[5] * dataset.RasterYSize

        if pixel_x_size == 0 or pixel_y_size == 0:
            print("Invalid pixel sizes")
            return False

        if min_x == 0.0 or min_y == 0.0 or max_x == 0.0 or max_y == 0.0:
            print("Invalid extent")
            return False

        # Set the source_pixel_size to wice the original resolution to compute the max scale
        source_pixel_size = pixel_x_size * 2.0

        # Set the max cell size to twice the cell size required for a super tile size of 512
        max_pixel_size = ((max_x - min_x) / 256 ) * 2.0

        min_scale = 0.0
        max_scale = 0.0

        for lod_info in self.tile_lod_info:
            print(lod_info[0], lod_info[1])
            if source_pixel_size > lod_info[0]:
                break
            max_scale = lod_info[1]

        for lod_info in self.tile_lod_info:
            print(lod_info[0], lod_info[1])
            if max_pixel_size > lod_info[0]:
                break
            min_scale = lod_info[1]

        dataset = None

        arcmap_bin_dir = os.path.dirname(sys.executable)
        arcmap_dir = os.path.dirname(arcmap_bin_dir)
        arcmap_tilingscheme_dir = os.path.join(arcmap_dir, 'TilingSchemes', 'gpkg_scheme.xml')
        #os.path.join(arcmap_tilingscheme_dir, '/gpkg_scheme.xml')

        arcpy.ManageTileCache_management(in_cache_location=tempFolder,
                                         manage_mode='RECREATE_ALL_TILES',
                                         in_cache_name=cacheName,
                                         in_datasource=filename,
                                         tiling_scheme='IMPORT_SCHEME',
                                         import_tiling_scheme=arcmap_tilingscheme_dir,
                                         max_cached_scale=max_scale,
                                         min_cached_scale=min_scale)

        
        cachePath = tempFolder + "/" + cacheName
        cache2gpkg.cache2gpkg(cachePath, self.filename, True)

        print("GeoPackage created")

        #shutil.rmtree(tempFolder)
        tempFolder = None
        return True


    def write_level(self, dataset, table_name, zoom_level, overview_info):
        """
        Write one zoom/resolution level into pyramid data table.
        @param dataset: Input dataset.
        @param table_name: Name of table to write pyramid data into.
        @param zoom_level: Zoom/Resolution level to write.
        @param overview_level: Index to overview to use.
        @param matrix_width: Number of tiles in X.
        @param matrix_height: Number of tiles in Y.
        @return: True on success, False on failure.
        """
        for tile_row in range(overview_info.matrix_height):
            for tile_column in range(overview_info.matrix_width):
                if not self.write_tile(dataset,
                                       table_name, zoom_level,
                                       tile_row, tile_column, overview_info):
                    print("Error writing full resolution image tiles to database.")
                    return False
        return True

    def write_tile(self, dataset,
                   table_name, zoom_level,
                   tile_row, tile_column, overview_info):
        """
        Extract specified tile from source dataset and write as a blob into GeoPackage, expanding colormap if required.
        @param dataset: Input dataset.
        @param table_name: Name of table to write pyramid data into.
        @param zoom_level: Zoom/Resolution level to write.
        @param tile_row: Tile index (Y).
        @param tile_column: Tile index (X).
        @return: True on success, False on failure.
        """
        out_band_count = dataset.RasterCount
        ulx = int(math.floor((tile_column * self.tile_width) * overview_info.factor_x))
        uly = int(math.floor(((tile_row * self.tile_height) * overview_info.factor_y)))
        input_tile_width = min(int(math.ceil(self.tile_width * overview_info.factor_x)), self.full_width - ulx)
        input_tile_height = min(int(math.ceil(self.tile_height * overview_info.factor_y)), self.full_height - uly)
        memory_dataset = self.mem_driver.Create('', self.tile_width, self.tile_height, out_band_count, self.data_type)
        if memory_dataset is None:
            print("Error creating temporary in-memory dataset for tile ", tile_column, ", ", tile_row,
                  " at zoom level ", zoom_level)
            return False
        data = dataset.ReadRaster(ulx, uly, input_tile_width, input_tile_height,
                                  self.tile_width, self.tile_height)
        memory_dataset.WriteRaster(0, 0, self.tile_width, self.tile_height, data)
        filename = tempfile.mktemp(suffix='tmp', prefix='gpkg_tile')
        output_dataset = self.driver.CreateCopy(filename, memory_dataset, 0)
        if output_dataset is None:
            print("Error creating temporary dataset for tile ", tile_column, ", ", tile_row,
                  " at zoom level ", zoom_level)
            return False
        output_dataset = None
        memory_dataset = None
        size = os.stat(filename).st_size
        if size == 0:
            print("Temporary dataset ", filename, "is 0 bytes.")
            os.unlink(filename)
            return False
        try:
            in_file = open(filename, 'rb')
            tile_data = in_file.read(size)
            in_file.close()
        except IOError as e:
            print("Error reading temporary dataset ", filename, ": ", e.args[0])
            os.unlink(filename)
            return False
        try:
            self.connection.execute(
                """
                INSERT INTO """ + table_name + """(zoom_level, tile_column, tile_row, tile_data)
                VALUES (?, ?, ?, ?);
                """,
                (zoom_level, tile_column, tile_row, buffer(tile_data))
            )
        except sqlite3.Error as e:
            print("Error inserting blob for tile ", tile_column, tile_row, ": ", e.args[0])
            return False
        os.unlink(filename)
        return True

    def open(self, filename):
        """
        Create or open a GeoPackage and create necessary tables and triggers.
        @param filename: Name of sqlite3 database.
        @return: True on success, False on failure.
        """
        self.filename = filename
        try:
            self.connection = sqlite3.connect(filename)
        except sqlite3.Error as e:
            print("Error opening ", filename, ": ", e.args[0])
            return False
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute(
                """
                PRAGMA application_id = 1196437808;
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys ( \
                             srs_name TEXT NOT NULL, \
                             srs_id INTEGER NOT NULL PRIMARY KEY, \
                             organization TEXT NOT NULL, \
                             organization_coordsys_id INTEGER NOT NULL, \
                             definition  TEXT NOT NULL, \
                             description TEXT );
                """
            )
            self.connection.execute(
                """
                INSERT INTO gpkg_spatial_ref_sys(srs_name,srs_id,organization,organization_coordsys_id,definition)
                SELECT 'Undefined Cartesian', -1, 'NONE', -1, 'undefined'
                WHERE NOT EXISTS(SELECT 1 FROM gpkg_spatial_ref_sys WHERE srs_id=-1);
                """
            )
            self.connection.execute(
                """
                INSERT INTO gpkg_spatial_ref_sys(srs_name,srs_id,organization,organization_coordsys_id,definition)
                SELECT 'Undefined Geographic', 0, 'NONE', 0, 'undefined'
                WHERE NOT EXISTS(SELECT 1 FROM gpkg_spatial_ref_sys WHERE srs_id=0);
                """
            )
            self.connection.execute(
                """
                INSERT INTO gpkg_spatial_ref_sys(srs_name,srs_id,organization,organization_coordsys_id,definition)
                SELECT 'WGS84', 4326, 'EPSG', 4326, 'GEOGCS["WGS 84",
                                                        DATUM["WGS_1984",
                                                            SPHEROID["WGS 84",6378137,298.257223563,
                                                                AUTHORITY["EPSG","7030"]],
                                                            AUTHORITY["EPSG","6326"]],
                                                        PRIMEM["Greenwich",0,
                                                            AUTHORITY["EPSG","8901"]],
                                                        UNIT["degree",0.01745329251994328,
                                                            AUTHORITY["EPSG","9122"]],
                                                        AUTHORITY["EPSG","4326"]]'
                WHERE NOT EXISTS(SELECT 1 FROM gpkg_spatial_ref_sys WHERE srs_id=4326);
                """
            )
            self.connection.execute(
                """
                    CREATE TABLE IF NOT EXISTS gpkg_contents (
                                 table_name TEXT NOT NULL PRIMARY KEY, \
                                 data_type TEXT NOT NULL, \
                                 identifier TEXT UNIQUE, \
                                 description TEXT DEFAULT '', \
                                 last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ',CURRENT_TIMESTAMP)), \
                                 min_x DOUBLE, \
                                 min_y DOUBLE, \
                                 max_x DOUBLE, \
                                 max_y DOUBLE, \
                                 srs_id INTEGER, \
                                 CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id) );
                    """
            )
            self.connection.execute(
                """
                    CREATE TABLE IF NOT EXISTS gpkg_tile_matrix (
                                 table_name TEXT NOT NULL,
                                 zoom_level INTEGER NOT NULL,
                                 matrix_width INTEGER NOT NULL,
                                 matrix_height INTEGER NOT NULL,
                                 tile_width INTEGER NOT NULL,
                                 tile_height INTEGER NOT NULL,
                                 pixel_x_size DOUBLE NOT NULL,
                                 pixel_y_size DOUBLE NOT NULL,
                                 CONSTRAINT pk_ttm PRIMARY KEY (table_name, zoom_level),
                                 CONSTRAINT fk_tmm_table_name FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name) );
                  """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_zoom_level_insert'
                    BEFORE INSERT ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'insert on table ''gpkg_tile_matrix'' violates constraint: zoom_level cannot be less than 0')
                    WHERE (NEW.zoom_level < 0);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_zoom_level_update'
                    BEFORE UPDATE OF zoom_level ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'update on table ''gpkg_tile_matrix'' violates constraint: zoom_level cannot be less than 0')
                    WHERE (NEW.zoom_level < 0);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_matrix_width_insert'
                    BEFORE INSERT ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'insert on table ''gpkg_tile_matrix'' violates constraint: matrix_width cannot be less than 1')
                    WHERE (NEW.matrix_width < 1);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_matrix_width_update'
                    BEFORE UPDATE OF matrix_width ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'update on table ''gpkg_tile_matrix'' violates constraint: matrix_width cannot be less than 1')
                    WHERE (NEW.matrix_width < 1);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_matrix_height_insert'
                    BEFORE INSERT ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'insert on table ''gpkg_tile_matrix'' violates constraint: matrix_height cannot be less than 1')
                    WHERE (NEW.matrix_height < 1);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_matrix_height_update'
                    BEFORE UPDATE OF matrix_height ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'update on table ''gpkg_tile_matrix'' violates constraint: matrix_height cannot be less than 1')
                    WHERE (NEW.matrix_height < 1);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_pixel_x_size_insert'
                    BEFORE INSERT ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'insert on table ''gpkg_tile_matrix'' violates constraint: pixel_x_size must be greater than 0')
                    WHERE NOT (NEW.pixel_x_size > 0);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_pixel_x_size_update'
                    BEFORE UPDATE OF pixel_x_size ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'update on table ''gpkg_tile_matrix'' violates constraint: pixel_x_size must be greater than 0')
                    WHERE NOT (NEW.pixel_x_size > 0);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_pixel_y_size_insert'
                    BEFORE INSERT ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'insert on table ''gpkg_tile_matrix'' violates constraint: pixel_y_size must be greater than 0')
                    WHERE NOT (NEW.pixel_y_size > 0);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TRIGGER IF NOT EXISTS 'gpkg_tile_matrix_pixel_y_size_update'
                    BEFORE UPDATE OF pixel_y_size ON 'gpkg_tile_matrix'
                    FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, 'update on table ''gpkg_tile_matrix'' violates constraint: pixel_y_size must be greater than 0')
                    WHERE NOT (NEW.pixel_y_size > 0);
                    END
                    """
            )
            self.connection.execute(
                """
                    CREATE TABLE IF NOT EXISTS gpkg_tile_matrix_set (
                                 table_name TEXT NOT NULL PRIMARY KEY,
                                 srs_id INTEGER NOT NULL,
                                 min_x DOUBLE NOT NULL,
                                 min_y DOUBLE NOT NULL,
                                 max_x DOUBLE NOT NULL,
                                 max_y DOUBLE NOT NULL,
                                 CONSTRAINT fk_gtms_table_name FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
                                 CONSTRAINT fk_gtms_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id) );
                  """
            )
            self.connection.commit()
        except sqlite3.Error as e:
            print("ERROR: SQLite error while creating core tables and triggers: ", e.args[0])
            return False
        return True


def usage():
    print("Usage: gdal2gpkg3 datasetname gpkgname")
    return 2


def equal(a, b):
    """
    Case insensitive string compare.
    @param a: String to compare.
    @param b: String to compare.
    @return: True if equal, False if not.
    """
    return a.lower() == b.lower()


def main(argv=None):
    dataset_filename = None
    gpkg_filename = None
    gpkg = GeoPackage()
    
    dataset_filename = arcpy.GetParameterAsText(0)
    gpkg_filename = arcpy.GetParameterAsText(1)
    
    image_extension = os.path.splitext(dataset_filename)[1][1:].strip()
    
    if image_extension.lower() in ('jpg', 'jpeg'):
        gpkg.format = "image/jpeg"
    elif image_extension.lower() == 'png':
        gpkg.format = "image/png"
    else:
        extension = ''    
    
    if not gpkg.open(gpkg_filename):
        print("ERROR: Failed to open or create ", gpkg_filename)
        return 1
    if not gpkg.add_dataset(dataset_filename):
        print("ERROR: Adding ", dataset_filename, " to ", gpkg_filename, " failed")
        return 1
    gpkg = None
    arcpy.AddMessage('\nDone')
    return 0


if __name__ == '__main__':
    version_num = int(gdal.VersionInfo('VERSION_NUM'))
    if version_num < 1800: # because of GetGeoTransform(can_return_null)
        print('ERROR: Python bindings of GDAL 1.8.0 or later required')
        sys.exit(1)
    sys.exit(main(sys.argv))
