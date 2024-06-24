#!/usr/bin/env python

import datetime
import json
import logging
import os
import sys
from collections import OrderedDict
from copy import deepcopy


import click
import pandas as pd


from . import utils

# Won't work on h:\
DEFAULT_WORKSPACE = r"D:/connections/GCOVERP@osa.sde"
curdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
DEFAULT_OUTPUT_DIR = os.path.abspath(os.path.join(curdir, "exports"))

GDB_PATH = "/media/marco/G12/GEODATA/20240503_0300_2030-12-31.gdb"
USED_SYMBOLS_PATH = os.path.join(DEFAULT_OUTPUT_DIR, "layer_used_symbols.json")

LAYERS_SYMBOL_RULES = "layer_symbols_rules.json"


COMMANDS_REQUIRING_ARCPY = ["export", "schema", "rules"]


def configure_logging(level):
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {level}")

    logging.basicConfig(
        level=numeric_level
    )  # Initial configuration (if no handlers exist)
    logging.getLogger().setLevel(
        numeric_level
    )  # Set the root logger's level explicitly
    logging.info(f"Logging level set to {level}")


def check_library():
    try:
        import arcpy

        return True
    except ImportError:
        return False


class MutuallyExclusiveOption(click.Option):
    def __init__(self, *args, **kwargs):
        self.mutually_exclusive = set(kwargs.pop("mutually_exclusive", []))
        help = kwargs.get("help", "")
        if self.mutually_exclusive:
            ex_str = ", ".join(self.mutually_exclusive)
            kwargs["help"] = help + (
                " NOTE: This argument is mutually exclusive with "
                " arguments: [" + ex_str + "]."
            )
        super().__init__(*args, **kwargs)

    def handle_parse_result(self, ctx, opts, args):
        current_opt = self.name
        if current_opt in opts and self.mutually_exclusive:
            for other_opt in self.mutually_exclusive:
                if other_opt in opts:
                    raise click.UsageError(
                        f"Illegal usage: `{current_opt}` is mutually exclusive with "
                        f"`{other_opt}`."
                    )
        return super().handle_parse_result(ctx, opts, args)


class CommandDisabled(click.ClickException):
    def __init__(self, message):
        super().__init__(message)


class ConditionalGroup(click.Group):
    def __init__(self, *args, **kwargs):
        self.condition = kwargs.pop("condition", lambda: True)
        super().__init__(*args, **kwargs)

    def get_command(self, ctx, cmd_name):
        if self.condition():
            return super().get_command(ctx, cmd_name)
        else:
            if cmd_name in COMMANDS_REQUIRING_ARCPY:
                raise CommandDisabled(
                    f"The command '{cmd_name}' is disabled because the 'arcpy' library is not installed."
                )
            return super().get_command(ctx, cmd_name)

    def invoke(self, ctx):
        try:
            super().invoke(ctx)
        except CommandDisabled as e:
            ctx.fail(e.message)


def _arg_split(ctx, param, value):
    # split columns by ',' and remove whitespace
    try:
        bbox = list(map(float, [c.strip() for c in value.split(",")]))
        # validate
        if len(bbox) != 4:
            raise ValueError("bbox must have four values")
    except ValueError as e:
        raise click.BadOptionUsage("resolution", f"--bbox: {e}")

    return bbox


@click.group(
    # cls=ConditionalGroup,
    # condition=check_library,
)
def geocover():
    """Command to work with TOPGIS/GeoCover ArcSDE database or ArcGis Pro project"""
    pass


@geocover.command(
    "export",
    context_settings={"show_default": True},
    help="Export ArcSDE data to generate the datamodel",
)
@click.option(
    "-l",
    "--log-level",
    default="INFO",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=True
    ),
    show_default=True,
    help="Log level",
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(exists=True, file_okay=False),
    help="The directory for the output",
    default=DEFAULT_OUTPUT_DIR,
)
@click.option(
    "-w",
    "--workspace",
    type=str,
    help="Workspace (SDE string or GDB)",
    default=DEFAULT_WORKSPACE,
)
def export(output_dir, workspace, log_level):
    import arcpy
    from . import  encoder 
    from . import schema

    now = datetime.datetime.now()

    arcpy.env.workspace = workspace

    logger = logging.getLogger(__name__)
    logger.setLevel(log_level.upper())
    logger.addHandler(logging.StreamHandler(sys.stdout))

    so = schema.GeocoverSchema.instance(workspace)

    encoder = encoder.ExtendedEncoder()

    connection_info = so.connection_info
    logger.debug(f"Connection info: {connection_info}")
    base_dict = {
        "date": now.strftime("%H:%M:%S-%d-%m-%Y"),
        "database": connection_info,
    }

    # Export coded domains
    coded_domains_dict = deepcopy(base_dict)

    coded_domains_dict.update(so.coded_domains)

    logger.info("Writting to 'coded_domains.json'...")
    utils.dump_dict_to_json(
        encoder.to_serializable_dict(so.coded_domains),
        os.path.join(output_dir, "coded_domains.json"),
    )

    # Export subtypes
    subtypes_dict = deepcopy(base_dict)

    subtypes_dict.update(so.subtypes)

    dump_dict_to_json(
        encoder.to_serializable_dict(subtypes_dict),
        os.path.join(output_dir, "subtypes_dict.json"),
    )

    logger.info("###### RELATIONSHIPS #########")

    logger.info(encoder.to_serializable_dict(so.relationships))

    # Export tables
    logger.info("###### TABLES #########")
    tables_dict = deepcopy(base_dict)

    tables_dict.update(so.tree_tables)

    dump_dict_to_json(
        encoder.to_serializable_dict(tables_dict),
        os.path.join(output_dir, "tables.json"),
    )
    with pd.ExcelWriter(os.path.join(output_dir, f"export_tables.xlsx")) as writer:
        for table_name, df in so.tree_tables.items():
            logger.info(table_name)

            prefix, short_name = table_name.split(".")
            short_name = short_name.replace("GC_", "").capitalize()

            if set(["PARENT_REf", "GEOL_CODE_INT"]).issubset(df.columns):
                try:
                    df["PARENT_REF"] = df["PARENT_REF"].fillna(0)
                    df.sort_values(by=["GEOL_CODE_INT", "PARENT_REF"], inplace=True)
                except KeyError as e:
                    logger.error(f"Table {table_name} has nokey: {e}")
                    continue

            logger.info(
                f"Exporting to {short_name}.csv, {short_name}.json and 'export_tables.xlsx' (sheet={short_name.upper()}"
            )
            try:
                df.to_csv(os.path.join(output_dir, f"{short_name}.csv"), index=True)
                df.to_json(os.path.join(output_dir, f"{short_name}.json"), index=True)
                df.to_excel(writer, sheet_name=short_name.upper())
            except PermissionError as e:
                logger.error(
                    f"Permission error: 'export_tables.xlsx' is probably already opened"
                )


@geocover.command(
    "schema", context_settings={"show_default": True}, help="Dumps ArcSDE schema"
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(exists=True, file_okay=False),
    help="The directory for the output",
    default=DEFAULT_OUTPUT_DIR,
)
@click.option(
    "-w",
    "--workspace",
    type=str,
    help="Workspace (SDE string or GDB)",
    default=DEFAULT_WORKSPACE,
)
@click.option(
    "-l",
    "--log-level",
    default="INFO",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=True
    ),
    show_default=True,
    help="Log level",
)
def schema(output_dir, workspace, log_level):
    import arcpy
    from . import  encoder 
    from . import schema

    arcpy.env.workspace = workspace

    logger = logging.getLogger(__name__)
    logger.setLevel(log_level.upper())
    logger.addHandler(logging.StreamHandler(sys.stdout))

    so = schema.GeocoverSchema.instance(workspace)

    encoder = encoder.ExtendedEncoder()

    connection_info = so.connection_info

    schema_dict = OrderedDict(
        {
            "datetime": datetime.datetime.now(),
            "relationships": so.relationships,
            "tables": so.tables,
            "featclasses": so.feature_classes,
            "domains": so.coded_domains,
            "subtypes": so.subtypes,
        }
    )

    logger.info("Writting to 'geocover-schema-sde.json'...")
    dump_dict_to_json(
        encoder.to_serializable_dict(schema_dict),
        os.path.join(output_dir, "geocover-schema-sde.json"),
    )


@geocover.command(
    "geolcode",
    context_settings={"show_default": True},
    help="Dumps all geol codes currently in use",
)
@click.option(
    "-o",
    "--output-file",
    type=click.Path(exists=False, file_okay=True, writable=True),
    default="../exports/all_geolcode.xlsx",
    help="The file for the output",
)
@click.option(
    "-w",
    "--workspace",
    type=str,
    help="Workspace (SDE string or GDB)",
    default=DEFAULT_WORKSPACE,
)
@click.option(
    "-l",
    "--log-level",
    default="INFO",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=True
    ),
    show_default=True,
    help="Log level",
)
def geolcode(output_file, workspace, log_level):
    from . import all_geolcodes 

    logger = logging.getLogger(__name__)
    logger.setLevel(log_level.upper())
    logger.addHandler(logging.StreamHandler(sys.stdout))

    ext = os.path.basename(os.path.abspath(output_file)).split(".")[1]

    df = all_geolcodes.get_geol_codes()

    json_str = df.to_dict(orient="records")

    logger.info(f"Saving to {output_file}")
    if ext == "json":
        df.to_json(
            path_or_buf=output_file,
            orient="records",
            force_ascii=False,
            indent=4,
            mode="w",
        )

    if ext == "xlsx":
        df.to_excel(output_file)


@geocover.command(
    "filter",
    context_settings={"show_default": True},
    help="Filter features by symbol rules",
)
@click.option(
    "-b",
    "--bbox",
    required=False,
    nargs=1,
    type=click.STRING,
    callback=_arg_split,
    cls=MutuallyExclusiveOption,
    mutually_exclusive=["geometry"],
    default="2725000,1158000,742500,1170000",  # Vals
    help="Filter with a bbox",
)
@click.option(
    "--geometry",
    type=click.Path(exists=True, file_okay=True),
    cls=MutuallyExclusiveOption,
    mutually_exclusive=["bbox"],
    help="Filter with a geojson geometry.",
    default="../exports/mapsheets.geojson",
)
@click.option(
    "--gdb-path", type=click.Path(exists=True), default=GDB_PATH, help="GDB file"
)
@click.option(
    "-o",
    "--output",
    type=click.Path(file_okay=True),
    default=USED_SYMBOLS_PATH,
    help="Used filtered symbols file",
)
@click.option(
    "-s",
    "--symbols",
    type=click.Path(exists=True, file_okay=True),
    help="The symbols file",
    required=True,
    default=LAYERS_SYMBOL_RULES,
)
@click.option(
    "-d",
    "--drop",
    default=False,
    help="Drop rules where features are absent",
    is_flag=True,
)
@click.option(
    "-l",
    "--log-level",
    default="INFO",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=True
    ),
    help="Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
)
def filter_symbols(bbox, geometry, gdb_path, output, log_level, symbols, drop):
    from shapely import box
    from . import filter_symbols 
    import geopandas as gpd
    import pandas as pd

    configure_logging(log_level)

    dirname = os.path.dirname(__file__)
    config_json = symbols
    now = datetime.datetime.now()

    results = {"bbox": bbox, "gdb_path": gdb_path, "datetime": now.isoformat()}

    logging.info(f"Mask: {geometry}")

    if geometry:
        try:
            # Read the mask file (shapefile or GeoJSON)
            mask_gdf = gpd.read_file(geometry)  # or 'path_to_mask_file.geojson'
            # Assuming the mask is a single geometry, you can dissolve to create a single unified geometry
            mask_geom = mask_gdf.unary_union
        except Exception as e:
            logging.error(e)
    else:
        mask_geom = box(*bbox)

    with open(config_json, "r") as f:
        layers = json.load(f)

    logging.debug(layers.keys())

    results["layers"] = filter_symbols.process_layers_symbols(layers, gdb_path, mask_geom)

    if output is not None:
        logging.info(f"Writing to {output}")

        if output.endswith(".json"):
            with open(output, "w", encoding="utf-8") as f:
                # Serialize the data and write it to the file
                json.dump(results, f, ensure_ascii=False, indent=4)
        elif output.endswith(".csv") or output.endswith(".xlsx"):
            df = pd.DataFrame.from_dict(results["layers"], orient="index")
            data = results["layers"]

            flattened_data = [
                (k1, k2, v)
                for k1, subdict in data.items()
                for k2, v in subdict.get("rules", {}).items()
            ]

            # Convert to a DataFrame
            df = pd.DataFrame(flattened_data, columns=["Layer", "Rule", "Count"])
            if drop:
                df = df[df.Count != 0]

            with pd.ExcelWriter(output) as writer:
                df.to_excel(writer, sheet_name="RULES")

    else:
        print(json.dumps(results, indent=4))


@geocover.command(
    "rules",
    context_settings={"show_default": True},
    help="Export ArcGis Pro layer symbology rules",
)
@click.option(
    "-w",
    "--workspace",
    type=click.Path(exists=True),
    default=r"D:\ArcGIS\GCOVERP.aprx",
    help="ESRI ArcGis Pro project with layers file",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(exists=False),
    default=DEFAULT_OUTPUT_DIR,
    help="Used symbols file",
)
@click.option(
    "-l",
    "--log-level",
    default="INFO",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=True
    ),
    help="Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
)
def export_rules(workspace, output, log_level):
    # Define the path to your ArcGIS Pro project
    import arcpy
    from . import  export_symbol_rules

    configure_logging(log_level)

    fields = ["OBJECTID", "MSH_MAP_TITLE", "MSH_MAP_NBR"]
    try:
        # Open the ArcGIS Pro project
        project_path = workspace
        aprx = arcpy.mp.ArcGISProject(workspace)
    except OSError as e:
        logging.error(f"Cannot open project {workspace}. Exiting: {e}")
        sys.exit()

    m = aprx.listMaps()[0]

    mapsheet_lyr = None

    res = {}
    for l in m.listLayers():
        if l.isFeatureLayer:
            attributes = export_symbol_rules.process_layers(l)
            res[l.name] = attributes

    output_fname = os.path.join(output, LAYERS_SYMBOL_RULES)

    logging.info(f"Writing to {output_fname}")

    with open(output_fname, "w", encoding="utf-8") as f:
        # Serialize the data and write it to the file
        json.dump(res, f, ensure_ascii=False, indent=4)


if __name__ == "__main__":
    geocover()
