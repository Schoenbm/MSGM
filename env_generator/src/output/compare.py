"""Validation de l'allocation Filosofi par rapport aux données IRIS 2022.

Méthode :
  1. Jointure spatiale : chaque bâtiment → IRIS qui le contient
  2. Agrégation : somme de population_allouee par IRIS
  3. Comparaison : pop_agregee_filosofi vs Ind_total (P22_POP) de l'IRIS
"""

import logging
from pathlib import Path

import contextily as ctx
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.cm as mcm
import pandas as pd

logger = logging.getLogger(__name__)

_OUTPUT_CSV = "validation.csv"
_OUTPUT_MAP = "validation_map.png"
_DPI = 150


def compare_iris_results(
    result_iris: gpd.GeoDataFrame,
    iris: gpd.GeoDataFrame,
    output_dir: str | Path,
) -> Path:
    """Valide l'allocation IRIS en agrégeant par IRIS et comparant à Ind_total.

    Pour chaque IRIS :
      - pop_allouee  : somme de population_allouee des bâtiments dans l'IRIS
      - pop_iris     : Ind_total de la source IRIS (recensement 2022)
      - diff         : pop_allouee - pop_iris
      - erreur_rel   : diff / pop_iris × 100  (%)

    Produit :
      - validation.csv     : table de validation par IRIS
      - validation_map.png : carte choroplèthe de l'erreur relative

    Args:
        result_iris : GeoDataFrame bâtiments avec population_allouee.
        iris        : GeoDataFrame IRIS avec Ind_total.
        output_dir  : Répertoire de sortie.

    Returns:
        Path vers le CSV de validation.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if result_iris.crs != iris.crs:
        result_iris = result_iris.to_crs(iris.crs)

    # Jointure bâtiments → IRIS (centroïde)
    centroids = result_iris.copy()
    centroids["geometry"] = result_iris.geometry.centroid

    joined = gpd.sjoin(
        centroids[["population_allouee", "geometry"]],
        iris[["CODE_IRIS", "geometry"]],
        how="left",
        predicate="within",
    )

    n_unmatched = joined["CODE_IRIS"].isna().sum()
    if n_unmatched > 0:
        logger.warning("%d bâtiments hors IRIS ignorés dans la validation", n_unmatched)

    # Agrégation par IRIS
    agg = (
        joined.dropna(subset=["CODE_IRIS"])
        .groupby("CODE_IRIS", as_index=False)["population_allouee"]
        .sum()
        .rename(columns={"population_allouee": "pop_allouee"})
    )

    # Fusion avec Ind_total — restreint aux IRIS ayant au moins un bâtiment alloué
    iris_stats = iris[["CODE_IRIS", "Ind_total", "geometry"]].rename(
        columns={"Ind_total": "pop_iris"}
    )
    merged = iris_stats.merge(agg, on="CODE_IRIS", how="inner")
    n_ignored = len(iris_stats) - len(merged)
    if n_ignored > 0:
        logger.info("%d IRIS sans bâtiment ignorés dans la comparaison", n_ignored)

    # Métriques
    merged["diff"] = merged["pop_allouee"] - merged["pop_iris"]
    has_pop = merged["pop_iris"] > 0
    merged["erreur_rel"] = 0.0
    merged.loc[has_pop, "erreur_rel"] = (
        merged.loc[has_pop, "diff"] / merged.loc[has_pop, "pop_iris"] * 100
    )

    # Résumé
    n = len(merged)
    n_pop = has_pop.sum()
    mae = merged.loc[has_pop, "diff"].abs().mean()
    mape = merged.loc[has_pop, "erreur_rel"].abs().mean()
    corr = merged.loc[has_pop, ["pop_allouee", "pop_iris"]].corr().iloc[0, 1]
    total_alloue = merged["pop_allouee"].sum()
    total_iris = merged["pop_iris"].sum()

    logger.info("=== VALIDATION IRIS vs IRIS 2022 ===")
    logger.info("IRIS comparés             : %d (dont %d avec pop > 0)", n, n_pop)
    logger.info("Population totale allouée : %d", int(total_alloue))
    logger.info("Population totale IRIS    : %d", int(total_iris))
    logger.info("Écart total               : %+d (%.2f%%)",
                int(total_alloue - total_iris),
                (total_alloue - total_iris) / total_iris * 100 if total_iris else 0)
    logger.info("Erreur absolue moyenne    : %.1f hab/IRIS", mae)
    logger.info("Erreur relative moyenne   : %.1f%%", mape)
    logger.info("Corrélation               : %.4f", corr)
    logger.info(
        "IRIS les plus sous-estimés :\n%s",
        merged.nsmallest(5, "diff")[["CODE_IRIS", "pop_allouee", "pop_iris", "diff", "erreur_rel"]]
        .to_string(index=False),
    )
    logger.info(
        "IRIS les plus sur-estimés :\n%s",
        merged.nlargest(5, "diff")[["CODE_IRIS", "pop_allouee", "pop_iris", "diff", "erreur_rel"]]
        .to_string(index=False),
    )

    # Export CSV
    csv_path = output_dir / _OUTPUT_CSV
    export_df = merged.drop(columns=["geometry"])
    export_df["CODE_IRIS"] = export_df["CODE_IRIS"].astype(str)
    export_df.to_csv(csv_path, index=False)
    logger.info("CSV validation : %s", csv_path)

    # Carte
    _make_iris_validation_map(merged, output_dir / _OUTPUT_MAP)

    return csv_path


def _make_iris_validation_map(merged: gpd.GeoDataFrame, out_path: Path) -> None:
    """Carte choroplèthe de l'erreur relative par IRIS (source iris)."""
    gdf = merged.to_crs(epsg=3857)

    vmax = min(100, float(gdf["erreur_rel"].abs().quantile(0.95)))
    if vmax == 0:
        vmax = 1.0
    cmap = plt.get_cmap("RdBu_r")
    norm = mcolors.TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)

    fig, ax = plt.subplots(figsize=(12, 10))

    has_pop = gdf["pop_iris"] > 0
    no_pop = gdf[~has_pop]
    with_pop = gdf[has_pop].copy()

    if not no_pop.empty:
        no_pop.plot(ax=ax, color="#cccccc", edgecolor="#999999", linewidth=0.3)
    if not with_pop.empty:
        with_pop["_color"] = with_pop["erreur_rel"].clip(-vmax, vmax).apply(
            lambda v: mcolors.to_hex(cmap(norm(v)))
        )
        with_pop.plot(ax=ax, color=with_pop["_color"].tolist(),
                      edgecolor="#555555", linewidth=0.3)

    try:
        ctx.add_basemap(ax, source=ctx.providers.CartoDB.Positron, zoom="auto")
    except Exception as e:
        logger.warning("Fond de carte indisponible : %s", e)

    sm = mcm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    fig.colorbar(sm, ax=ax, fraction=0.025, pad=0.02).set_label(
        "Erreur relative (%)", fontsize=10
    )
    ax.set_axis_off()
    ax.set_title(
        "Erreur relative allocation IRIS vs recensement IRIS 2022\n"
        "(bleu = sous-estimation, rouge = sur-estimation)",
        fontsize=12, pad=8,
    )

    fig.savefig(out_path, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    logger.info("Carte validation : %s", out_path)


def compare_results(
    result_filosofi: gpd.GeoDataFrame,
    iris: gpd.GeoDataFrame,
    output_dir: str | Path,
) -> Path:
    """Valide l'allocation Filosofi en agrégeant par IRIS et comparant à P22_POP.

    Pour chaque IRIS :
      - pop_filosofi  : somme de population_allouee des bâtiments dans l'IRIS
      - pop_iris_2022 : Ind_total du loader IRIS (= P22_POP recensement 2022)
      - diff          : pop_filosofi - pop_iris_2022
      - erreur_rel    : diff / pop_iris_2022 × 100  (%)

    Produit :
      - validation.csv     : table de validation par IRIS
      - validation_map.png : carte choroplèthe de l'erreur relative

    Args:
        result_filosofi : GeoDataFrame bâtiments avec population_allouee.
        iris            : GeoDataFrame IRIS avec Ind_total (P22_POP 2022).
        output_dir      : Répertoire de sortie.

    Returns:
        Path vers le CSV de validation.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Aligner les CRS ────────────────────────────────────────────────────
    if result_filosofi.crs != iris.crs:
        result_filosofi = result_filosofi.to_crs(iris.crs)

    # ── 2. Jointure bâtiments → IRIS (centroïde) ─────────────────────────────
    centroids = result_filosofi.copy()
    centroids["geometry"] = result_filosofi.geometry.centroid

    joined = gpd.sjoin(
        centroids[["population_allouee", "geometry"]],
        iris[["CODE_IRIS", "geometry"]],
        how="left",
        predicate="within",
    )

    n_unmatched = joined["CODE_IRIS"].isna().sum()
    if n_unmatched > 0:
        logger.warning("%d bâtiments hors IRIS ignorés dans la validation", n_unmatched)

    # ── 3. Agrégation par IRIS ────────────────────────────────────────────────
    agg = (
        joined.dropna(subset=["CODE_IRIS"])
        .groupby("CODE_IRIS", as_index=False)["population_allouee"]
        .sum()
        .rename(columns={"population_allouee": "pop_filosofi"})
    )

    # ── 4. Fusion avec données IRIS 2022 ──────────────────────────────────────
    iris_stats = iris[["CODE_IRIS", "Ind_total", "geometry"]].rename(
        columns={"Ind_total": "pop_iris_2022"}
    )
    merged = iris_stats.merge(agg, on="CODE_IRIS", how="left")
    merged["pop_filosofi"] = merged["pop_filosofi"].fillna(0)

    # ── 5. Métriques ──────────────────────────────────────────────────────────
    merged["diff"] = merged["pop_filosofi"] - merged["pop_iris_2022"]
    has_pop = merged["pop_iris_2022"] > 0
    merged["erreur_rel"] = 0.0
    merged.loc[has_pop, "erreur_rel"] = (
        merged.loc[has_pop, "diff"] / merged.loc[has_pop, "pop_iris_2022"] * 100
    )

    # ── 6. Résumé ─────────────────────────────────────────────────────────────
    n = len(merged)
    n_pop = has_pop.sum()
    mae = merged.loc[has_pop, "diff"].abs().mean()
    mape = merged.loc[has_pop, "erreur_rel"].abs().mean()
    corr = merged.loc[has_pop, ["pop_filosofi", "pop_iris_2022"]].corr().iloc[0, 1]
    total_filosofi = merged["pop_filosofi"].sum()
    total_iris = merged["pop_iris_2022"].sum()

    logger.info("=== VALIDATION FILOSOFI vs IRIS 2022 ===")
    logger.info("IRIS comparés              : %d (dont %d avec pop > 0)", n, n_pop)
    logger.info("Population totale Filosofi : %d", int(total_filosofi))
    logger.info("Population totale IRIS     : %d", int(total_iris))
    logger.info("Écart total                : %+d (%.1f%%)",
                int(total_filosofi - total_iris),
                (total_filosofi - total_iris) / total_iris * 100 if total_iris else 0)
    logger.info("Erreur absolue moyenne     : %.1f hab/IRIS", mae)
    logger.info("Erreur relative moyenne    : %.1f%%", mape)
    logger.info("Corrélation                : %.4f", corr)
    logger.info(
        "IRIS les plus sous-estimés (Filosofi < IRIS 2022) :\n%s",
        merged.nsmallest(5, "diff")[["CODE_IRIS", "pop_filosofi", "pop_iris_2022", "diff", "erreur_rel"]]
        .to_string(index=False),
    )
    logger.info(
        "IRIS les plus sur-estimés (Filosofi > IRIS 2022) :\n%s",
        merged.nlargest(5, "diff")[["CODE_IRIS", "pop_filosofi", "pop_iris_2022", "diff", "erreur_rel"]]
        .to_string(index=False),
    )

    # ── 7. Export CSV ─────────────────────────────────────────────────────────
    csv_path = output_dir / _OUTPUT_CSV
    export_df = merged.drop(columns=["geometry"])
    export_df["CODE_IRIS"] = export_df["CODE_IRIS"].astype(str)
    export_df.to_csv(csv_path, index=False)
    logger.info("CSV validation : %s", csv_path)

    # ── 8. Carte ──────────────────────────────────────────────────────────────
    _make_validation_map(merged, output_dir / _OUTPUT_MAP)

    return csv_path


def _make_validation_map(merged: gpd.GeoDataFrame, out_path: Path) -> None:
    """Carte choroplèthe de l'erreur relative par IRIS."""
    gdf = merged.to_crs(epsg=3857)

    vmax = min(100, float(gdf["erreur_rel"].abs().quantile(0.95)))
    cmap = plt.get_cmap("RdBu_r")
    norm = mcolors.TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)

    fig, axes = plt.subplots(1, 2, figsize=(22, 10))

    # ── Panneau gauche : populations absolues côte à côte ────────────────────
    ax_abs = axes[0]
    vmax_pop = int(gdf[["pop_filosofi", "pop_iris_2022"]].max().max())
    cmap_pop = mcolors.LinearSegmentedColormap.from_list(
        "pop", ["#ffffb2", "#fecc5c", "#fd8d3c", "#f03b20", "#bd0026"]
    )
    norm_pop = mcolors.Normalize(vmin=0, vmax=vmax_pop)

    gdf["_color_abs"] = gdf["pop_filosofi"].apply(
        lambda v: mcolors.to_hex(cmap_pop(norm_pop(v)))
    )
    gdf.plot(ax=ax_abs, color=gdf["_color_abs"].tolist(),
             edgecolor="#555555", linewidth=0.3)
    ctx.add_basemap(ax_abs, source=ctx.providers.CartoDB.Positron, zoom="auto")
    sm_abs = mcm.ScalarMappable(cmap=cmap_pop, norm=norm_pop)
    sm_abs.set_array([])
    fig.colorbar(sm_abs, ax=ax_abs, fraction=0.025, pad=0.02).set_label(
        "Population (Filosofi agrégé)", fontsize=10
    )
    ax_abs.set_axis_off()
    ax_abs.set_title("Population Filosofi agrégée par IRIS", fontsize=12, pad=8)

    # ── Panneau droit : erreur relative ──────────────────────────────────────
    ax_err = axes[1]
    has_pop = gdf["pop_iris_2022"] > 0
    no_pop = gdf[~has_pop]
    with_pop = gdf[has_pop].copy()

    if not no_pop.empty:
        no_pop.plot(ax=ax_err, color="#cccccc", edgecolor="#999999", linewidth=0.3)
    if not with_pop.empty:
        with_pop["_color_err"] = with_pop["erreur_rel"].clip(-vmax, vmax).apply(
            lambda v: mcolors.to_hex(cmap(norm(v)))
        )
        with_pop.plot(ax=ax_err, color=with_pop["_color_err"].tolist(),
                      edgecolor="#555555", linewidth=0.3)

    ctx.add_basemap(ax_err, source=ctx.providers.CartoDB.Positron, zoom="auto")
    sm_err = mcm.ScalarMappable(cmap=cmap, norm=norm)
    sm_err.set_array([])
    fig.colorbar(sm_err, ax=ax_err, fraction=0.025, pad=0.02).set_label(
        "Erreur relative (%)", fontsize=10
    )
    ax_err.set_axis_off()
    ax_err.set_title(
        "Erreur relative Filosofi vs IRIS 2022\n(bleu = sous-estimation, rouge = sur-estimation)",
        fontsize=12, pad=8,
    )

    fig.suptitle(
        "Validation de l'allocation Filosofi — Métropole grenobloise",
        fontsize=14, y=1.01,
    )
    fig.savefig(out_path, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    logger.info("Carte validation : %s", out_path)
