"""Comparaison de l'allocation IRIS agrégée par carreaux Filosofi 200m."""

import logging
from pathlib import Path

import contextily as ctx
import geopandas as gpd
import matplotlib.colors as mcolors
import matplotlib.cm as mcm
import matplotlib.pyplot as plt
import pandas as pd

logger = logging.getLogger(__name__)

_DPI = 150


def compare_iris_vs_carreaux(
    result_iris: gpd.GeoDataFrame,
    carreaux: gpd.GeoDataFrame,
    output_dir: str | Path,
) -> Path:
    """Agrège la population IRIS par carreau Filosofi et cartographie les écarts.

    Pour chaque carreau Filosofi :
      - pop_iris    : somme de population_allouee (pipeline IRIS) des bâtiments dedans
      - pop_filo    : Ind_total du carreau Filosofi
      - diff        : pop_iris - pop_filo
      - erreur_rel  : diff / pop_filo × 100  (%)

    Args:
        result_iris : GeoDataFrame bâtiments avec population_allouee (pipeline IRIS).
        carreaux    : GeoDataFrame carreaux Filosofi avec Ind_total.
        output_dir  : Répertoire de sortie.

    Returns:
        Path vers le CSV produit.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Aligner CRS
    if result_iris.crs != carreaux.crs:
        result_iris = result_iris.to_crs(carreaux.crs)

    # 2. Jointure centroïdes bâtiments → carreaux
    centroids = result_iris.copy()
    centroids["geometry"] = result_iris.geometry.centroid

    joined = gpd.sjoin(
        centroids[["population_allouee", "geometry"]],
        carreaux[["Idcar_200m", "Ind_total", "geometry"]],
        how="left",
        predicate="within",
    )

    n_hors = joined["Idcar_200m"].isna().sum()
    if n_hors > 0:
        logger.warning("%d bâtiments IRIS hors carreaux Filosofi ignorés", n_hors)

    # 3. Agrégation par carreau
    agg = (
        joined.dropna(subset=["Idcar_200m"])
        .groupby("Idcar_200m", as_index=False)["population_allouee"]
        .sum()
        .rename(columns={"population_allouee": "pop_iris"})
    )

    # 4. Fusion avec carreaux Filosofi
    merged = carreaux[["Idcar_200m", "Ind_total", "geometry"]].merge(
        agg, on="Idcar_200m", how="left"
    )
    merged["pop_iris"] = merged["pop_iris"].fillna(0)
    merged = merged.rename(columns={"Ind_total": "pop_filo"})

    # 5. Métriques
    merged["diff"] = merged["pop_iris"] - merged["pop_filo"]
    has_pop = merged["pop_filo"] > 0
    merged["erreur_rel"] = 0.0
    merged.loc[has_pop, "erreur_rel"] = (
        merged.loc[has_pop, "diff"] / merged.loc[has_pop, "pop_filo"] * 100
    )

    # 6. Résumé
    mae = merged.loc[has_pop, "diff"].abs().mean()
    mape = merged.loc[has_pop, "erreur_rel"].abs().mean()
    corr = merged.loc[has_pop, ["pop_iris", "pop_filo"]].corr().iloc[0, 1]
    logger.info("=== IRIS vs Carreaux Filosofi ===")
    logger.info("Carreaux comparés   : %d (dont %d avec pop > 0)", len(merged), has_pop.sum())
    logger.info("Pop IRIS agrégée    : %d", int(merged["pop_iris"].sum()))
    logger.info("Pop Filosofi totale : %d", int(merged["pop_filo"].sum()))
    logger.info("Écart total         : %+d", int(merged["pop_iris"].sum() - merged["pop_filo"].sum()))
    logger.info("MAE                 : %.1f hab/carreau", mae)
    logger.info("MAPE                : %.1f%%", mape)
    logger.info("Corrélation         : %.4f", corr)

    # 7. Export CSV
    csv_path = output_dir / "compare_grid.csv"
    merged.drop(columns=["geometry"]).to_csv(csv_path, index=False)

    # 8. Carte
    map_path = output_dir / "compare_grid_map.png"
    _make_map(merged, map_path)

    return csv_path


def _discrete_cmap_norm(bins: list[float]):
    """Retourne (cmap, norm) discrets sur les bins fournis.

    Bleus pour les négatifs, oranges/rouges pour les positifs,
    jaune-vert pour la plage centrale — pas de blanc.
    """
    n = len(bins) - 1
    # Palette sans blanc : bleus saturés → vert-jaune → oranges saturés
    colors = [
        "#08519c", "#2171b5", "#6baed6", "#bdd7e7",   # bleus (négatifs forts → faibles)
        "#74c476",                                       # vert (autour de 0)
        "#fdae6b", "#f16913", "#d94801", "#7f2704",    # oranges/rouges (positifs faibles → forts)
    ]
    # Adapter le nb de couleurs au nb de bins
    if n <= len(colors):
        half = n // 2
        neg_colors = colors[:half]
        pos_colors = colors[-(n - half):]
        palette = neg_colors + pos_colors
    else:
        palette = colors
    cmap = mcolors.ListedColormap(palette[:n])
    norm = mcolors.BoundaryNorm(bins, cmap.N)
    return cmap, norm


def _make_map(merged: gpd.GeoDataFrame, out_path: Path) -> None:
    gdf = merged.to_crs(epsg=3857)
    has_pop = gdf["pop_filo"] > 0
    no_pop  = gdf[~has_pop]
    with_pop = gdf[has_pop].copy()

    # Bins discrets — écart absolu (hab)
    bins_abs = [-500, -200, -100, -50, -20, 20, 50, 100, 200, 500]
    cmap_abs, norm_abs = _discrete_cmap_norm(bins_abs)

    # Bins discrets — écart relatif (%)
    bins_rel = [-150, -75, -40, -20, -5, 5, 20, 40, 75, 150]
    cmap_rel, norm_rel = _discrete_cmap_norm(bins_rel)

    fig, axes = plt.subplots(1, 2, figsize=(22, 10))

    for ax, col, cmap, norm, bins, label, title in [
        (axes[0], "diff", cmap_abs, norm_abs, bins_abs,
         "Écart absolu (hab)\nIRIS − Filosofi",
         "Écart absolu par carreau 200m"),
        (axes[1], "erreur_rel", cmap_rel, norm_rel, bins_rel,
         "Écart relatif (%)\n(IRIS − Filosofi) / Filosofi",
         "Écart relatif par carreau 200m"),
    ]:
        if not no_pop.empty:
            no_pop.plot(ax=ax, color="#cccccc", edgecolor="none")
        if not with_pop.empty:
            with_pop.plot(ax=ax, column=col, cmap=cmap, norm=norm,
                          edgecolor="none", legend=False)

        try:
            ctx.add_basemap(ax, source=ctx.providers.CartoDB.Positron, zoom="auto")
        except Exception:
            pass

        sm = mcm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        cb = fig.colorbar(sm, ax=ax, fraction=0.025, pad=0.02,
                          ticks=bins, spacing="uniform")
        cb.set_label(label, fontsize=10)
        cb.ax.set_yticklabels([str(b) for b in bins], fontsize=8)
        ax.set_axis_off()
        ax.set_title(
            f"{title}\n(bleu = IRIS sous-estime, rouge = IRIS surestime)",
            fontsize=11, pad=8,
        )

    fig.suptitle(
        "Population IRIS agrégée vs Filosofi — par carreau 200m — Métropole grenobloise",
        fontsize=13, y=1.01,
    )
    fig.savefig(out_path, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    logger.info("Carte grille : %s", out_path)
