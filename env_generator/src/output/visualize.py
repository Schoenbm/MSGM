import logging
from pathlib import Path

import contextily as ctx
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.cm as mcm
from matplotlib.colorbar import ColorbarBase

logger = logging.getLogger(__name__)

_OUTPUT_FILE = "map.png"
_DPI = 150


def make_map(result: gpd.GeoDataFrame, output_dir: str | Path) -> Path:
    """Generate a static PNG map with buildings coloured by population.

    Buildings are reprojected to Web Mercator (EPSG:3857), rendered as
    polygons with a continuous colour gradient (yellow → dark red) proportional
    to population_allouee, over a CartoDB Positron basemap.

    Args:
        result: GeoDataFrame with geometry and population_allouee columns.
        output_dir: Directory where map.png is written.

    Returns:
        Path to the generated PNG file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    gdf = result[["ID", "NB_LOGTS", "population_allouee", "geometry"]].copy()
    gdf = gdf.to_crs(epsg=3857)  # Web Mercator required by contextily

    pop = gdf["population_allouee"]
    vmax = int(pop.quantile(0.99))
    vmax = max(vmax, 1)

    cmap = mcolors.LinearSegmentedColormap.from_list(
        "pop", ["#ffffb2", "#fecc5c", "#fd8d3c", "#f03b20", "#bd0026"]
    )
    norm = mcolors.Normalize(vmin=0, vmax=vmax)

    fig, ax = plt.subplots(figsize=(16, 14))

    # Buildings with population = 0 → grey
    no_pop = gdf[pop == 0]
    if not no_pop.empty:
        no_pop.plot(ax=ax, color="#cccccc", edgecolor="#999999", linewidth=0.2)

    # Buildings with population > 0 → colour gradient
    with_pop = gdf[pop > 0].copy()
    if not with_pop.empty:
        with_pop["color"] = with_pop["population_allouee"].clip(upper=vmax).apply(
            lambda v: mcolors.to_hex(cmap(norm(v)))
        )
        with_pop.plot(
            ax=ax,
            color=with_pop["color"].tolist(),
            edgecolor="#555555",
            linewidth=0.2,
        )

    # Basemap tiles (CartoDB Positron — light, minimal)
    try:
        ctx.add_basemap(ax, source=ctx.providers.CartoDB.Positron, zoom="auto")
    except Exception as e:
        logger.warning("Fond de carte indisponible (pas de réseau ?) : %s", e)

    # Colourbar
    sm = mcm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.025, pad=0.02)
    cbar.set_label("Population estimée par bâtiment", fontsize=11)

    ax.set_axis_off()
    ax.set_title("Distribution de la population — Métropole grenobloise", fontsize=14, pad=12)

    out_path = output_dir / _OUTPUT_FILE
    fig.savefig(out_path, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)

    logger.info("Carte statique generee : %s", out_path)
    return out_path
