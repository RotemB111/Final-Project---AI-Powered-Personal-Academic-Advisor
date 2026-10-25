"""
analytics_pipeline_final.py

Purpose:
- The schedule score function already exists.
- This analytics pipeline uses Machine Learning to learn the relative weights
  of the scoring criteria, based on real user choices.

Project logic:
- Each session can display 1 to 3 generated schedules to the user.
- If exactly one schedule is selected=True, this is a valid labeled session.
  The selected schedule is positive, and the other schedules in that same
  session are negative because they were viewed and not chosen.
- If all schedules in a session are selected=False, this is treated as
  unlabeled data. It does NOT mean the schedules are bad; it only means
  no clear choice exists.
- If more than one schedule is selected=True in the same session, this is invalid.
- If more than 3 schedules exist in one session, this is treated as a data error.

Main outputs:
1. Data quality summary.
2. Model comparison table.
3. Best model selected automatically by metrics.
4. Criterion weights learned from the best model.
5. Optional save of the selected weights to Supabase table ml_criterion_weights.
6. Optional semi-supervised pseudo-labeling for unlabeled sessions.
7. Graphs for project presentation:
   - Correlation matrix between criteria and user selection
   - Data quality by session type
   - Model performance comparison
   - Criterion importance comparison by model
   - Final optimizer weights
   - ROC curve comparison
   - Confusion matrices for all trained models

Required Supabase table for saving weights:

create table if not exists public.ml_criterion_weights (
  weight_id bigint generated always as identity primary key,
  model_name text not null,
  weight_g numeric,
  weight_d numeric,
  weight_ld numeric,
  weight_t numeric,
  weight_lw numeric,
  weight_c numeric,
  weight_a numeric,
  num_training_records integer,
  accuracy numeric,
  precision numeric,
  recall numeric,
  f1_score numeric,
  auc numeric,
  semi_supervised_used boolean default false,
  created_at timestamp with time zone default now()
);
"""

import os
import warnings
from datetime import datetime
from typing import Dict, Tuple, List

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from dotenv import load_dotenv
from supabase import create_client, Client

from sklearn.model_selection import GridSearchCV, StratifiedKFold
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    roc_auc_score,
    confusion_matrix,
    roc_curve,
)

try:
    from xgboost import XGBClassifier
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False

warnings.filterwarnings("ignore")


# =========================================================
# Configuration
# =========================================================
TABLE_NAME = "generated_schedules"
SESSION_COLUMN = "session_id"
WEIGHTS_TABLE_NAME = "ml_criterion_weights"

# Set to True after creating the ml_criterion_weights table in Supabase.
SAVE_WEIGHTS_TO_DB = True

# Semi-supervised mode:
# Uses sessions with no selected schedule only through careful pseudo-labeling.
USE_SEMI_SUPERVISED = True
PSEUDO_LABEL_MIN_PROB = 0.75
PSEUDO_LABEL_MIN_MARGIN = 0.20

RANDOM_STATE = 42
TEST_SIZE = 0.20
MIN_SELECTED_SCHEDULES = 5
SUPABASE_PAGE_SIZE = 1000

# Sessions that showed only one schedule are not used: there was no real choice.
MIN_ALTERNATIVES_FOR_TRAINING = 2

# Retraining cadence: all models are retrained every CHECKPOINT_STEP new labeled sessions
# (sessions where the user selected exactly one schedule).
CHECKPOINT_STEP = 100

# Graph output
SAVE_PLOTS_TO_FILES = True
SHOW_PLOTS = True
PLOTS_OUTPUT_DIR = "analytics_outputs"

FEATURE_COLUMNS = [
    "score_g",
    "score_d",
    "score_ld",
    "score_t",
    "score_lw",
    "score_c",
    "score_a",
]

FEATURE_NAMES = ["G", "D", "LD", "T", "LW", "C", "A"]


# =========================================================
# Supabase
# =========================================================
def get_supabase_client() -> Client:
    load_dotenv()
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY in .env file")

    return create_client(supabase_url, supabase_key)


def fetch_schedules(supabase: Client) -> pd.DataFrame:
    print("🚀 Connecting to Supabase and fetching generated schedules...")
    # Supabase returns at most 1000 rows per request, so read page by page.
    rows, start = [], 0
    while True:
        page = supabase.table(TABLE_NAME).select("*").range(start, start + SUPABASE_PAGE_SIZE - 1).execute().data or []
        rows += page
        if len(page) < SUPABASE_PAGE_SIZE:
            break
        start += SUPABASE_PAGE_SIZE

    if not rows:
        raise ValueError(f"No data found in table: {TABLE_NAME}")

    df = pd.DataFrame(rows)
    print(f"📥 Loaded {len(df)} schedule records from database")
    return df


# =========================================================
# Data preparation
# =========================================================
def validate_required_columns(df: pd.DataFrame) -> None:
    required = FEATURE_COLUMNS + ["is_selected", SESSION_COLUMN]
    missing = [col for col in required if col not in df.columns]

    if missing:
        raise ValueError(f"Missing required columns in {TABLE_NAME}: {missing}")


def normalize_is_selected(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    mapping = {
        True: 1,
        False: 0,
        "true": 1,
        "false": 0,
        "True": 1,
        "False": 0,
        "TRUE": 1,
        "FALSE": 0,
        1: 1,
        0: 0,
        "1": 1,
        "0": 0,
    }

    df["is_selected_clean"] = df["is_selected"].map(mapping)
    return df


def prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in FEATURE_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    before = len(df)
    df = df.dropna(subset=FEATURE_COLUMNS + ["is_selected_clean", SESSION_COLUMN])
    after = len(df)

    if before != after:
        print(f"⚠️ Removed {before - after} records with missing features, labels, or session_id")

    # Your criteria are already in the same range, 0 to 1.
    # Therefore, no StandardScaler is applied.
    return df


def split_sessions_by_quality(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Session classification for this project:
    - A session may contain 1 to 3 generated schedules. Fewer than 3 is allowed.
    - More than 3 schedules is not allowed and is treated as a data error.
    - Exactly one selected schedule means the session is labeled and usable for training.
    - Zero selected schedules means the session is unlabeled.
    - More than one selected schedule means the session is invalid.
    """
    valid_labeled_groups = []
    unlabeled_groups = []
    invalid_groups = []
    too_many_schedules_groups = []
    single_alternative_groups = []

    for session_id, group in df.groupby(SESSION_COLUMN):
        group = group.copy()
        session_size = len(group)
        selected_count = int(group["is_selected_clean"].sum())

        if session_size > 3:
            group["label_source"] = "invalid_too_many_schedules"
            too_many_schedules_groups.append(group)
        elif session_size < MIN_ALTERNATIVES_FOR_TRAINING:
            # A single shown schedule has nothing to be compared with, so selecting it
            # carries no preference signal; kept out of both training and pseudo-labeling.
            group["label_source"] = "single_alternative"
            single_alternative_groups.append(group)
        elif selected_count == 1:
            group["label_source"] = "real"
            valid_labeled_groups.append(group)
        elif selected_count == 0:
            group["label_source"] = "unlabeled"
            unlabeled_groups.append(group)
        else:
            group["label_source"] = "invalid_multiple_selected"
            invalid_groups.append(group)

    valid_labeled_df = concat_or_empty(valid_labeled_groups, df)
    unlabeled_df = concat_or_empty(unlabeled_groups, df)
    invalid_df = concat_or_empty(invalid_groups, df)
    too_many_schedules_df = concat_or_empty(too_many_schedules_groups, df)

    print_data_quality_summary(df, valid_labeled_df, unlabeled_df, invalid_df, too_many_schedules_df)
    print(f"⚪ Skipped sessions with fewer than {MIN_ALTERNATIVES_FOR_TRAINING} alternatives: {len(single_alternative_groups)}")
    return valid_labeled_df, unlabeled_df, invalid_df, too_many_schedules_df


def concat_or_empty(groups: List[pd.DataFrame], template_df: pd.DataFrame) -> pd.DataFrame:
    if groups:
        return pd.concat(groups, ignore_index=True)

    empty = pd.DataFrame(columns=list(template_df.columns) + ["label_source"])
    return empty


def print_data_quality_summary(
    df: pd.DataFrame,
    valid_labeled_df: pd.DataFrame,
    unlabeled_df: pd.DataFrame,
    invalid_df: pd.DataFrame,
    too_many_schedules_df: pd.DataFrame,
) -> None:
    print("\n" + "=" * 80)
    print("📊 Data Quality Summary")
    print("=" * 80)
    print(f"Total records: {len(df)}")
    print(f"Total sessions: {df[SESSION_COLUMN].nunique()}")
    print(f"✅ Valid labeled sessions, exactly 1 True, session size 1-3: {count_sessions(valid_labeled_df)}")
    print(f"🟡 Unlabeled sessions, no selected schedule, session size 1-3: {count_sessions(unlabeled_df)}")
    print(f"❌ Invalid sessions, more than 1 True: {count_sessions(invalid_df)}")
    print(f"⚠️ Invalid sessions, more than 3 schedules: {count_sessions(too_many_schedules_df)}")

    if not valid_labeled_df.empty:
        print("\nClass distribution in valid labeled records:")
        counts = valid_labeled_df["is_selected_clean"].value_counts().rename(
            index={0: "Rejected but viewed", 1: "Selected"}
        )
        print(counts.to_string())


def count_sessions(df: pd.DataFrame) -> int:
    if df.empty or SESSION_COLUMN not in df.columns:
        return 0
    return int(df[SESSION_COLUMN].nunique())


# =========================================================
# Session-level train/test split
# =========================================================
def split_train_test_by_session(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """
    Keeps all 3 schedules from the same session together.
    This prevents data leakage between train and test.
    """
    session_ids = df[SESSION_COLUMN].drop_duplicates().to_numpy().copy()

    rng = np.random.default_rng(RANDOM_STATE)
    rng.shuffle(session_ids)

    test_size_sessions = max(1, int(round(len(session_ids) * TEST_SIZE)))
    test_sessions = set(session_ids[:test_size_sessions])
    train_sessions = set(session_ids[test_size_sessions:])

    train_df = df[df[SESSION_COLUMN].isin(train_sessions)].copy()
    test_df = df[df[SESSION_COLUMN].isin(test_sessions)].copy()

    X_train = train_df[FEATURE_COLUMNS]
    y_train = train_df["is_selected_clean"].astype(int)
    X_test = test_df[FEATURE_COLUMNS]
    y_test = test_df["is_selected_clean"].astype(int)

    if y_train.nunique() < 2 or y_test.nunique() < 2:
        raise ValueError(
            "Train/Test split contains only one class. Add more valid sessions or reduce TEST_SIZE."
        )

    print("\n" + "=" * 80)
    print("🔀 Train/Test Split by Session")
    print("=" * 80)
    print(f"Train sessions: {len(train_sessions)} | Train records: {len(train_df)}")
    print(f"Test sessions: {len(test_sessions)} | Test records: {len(test_df)}")

    return X_train, X_test, y_train, y_test


# =========================================================
# Models
# =========================================================
def get_cv_splits(y_train: pd.Series) -> int:
    min_class_count = int(y_train.value_counts().min())
    return max(2, min(3, min_class_count))


def get_models(y_train: pd.Series) -> Dict[str, object]:
    selected_count = int(y_train.sum())
    rejected_count = int(len(y_train) - selected_count)
    scale_pos_weight = rejected_count / selected_count if selected_count > 0 else 1
    cv_splits = get_cv_splits(y_train)

    models: Dict[str, object] = {
        "Logistic Regression": LogisticRegression(
            random_state=RANDOM_STATE,
            class_weight="balanced",
            max_iter=2000,
        ),
        "Random Forest": GridSearchCV(
            estimator=RandomForestClassifier(
                random_state=RANDOM_STATE,
                class_weight="balanced",
            ),
            param_grid={
                "n_estimators": [100, 200],
                "max_depth": [3, 5, None],
                "min_samples_split": [2, 5],
                "min_samples_leaf": [1, 2],
            },
            cv=StratifiedKFold(n_splits=cv_splits, shuffle=True, random_state=RANDOM_STATE),
            scoring="roc_auc",
            n_jobs=-1,
        ),
    }

    if XGBOOST_AVAILABLE:
        models["XGBoost"] = XGBClassifier(
            random_state=RANDOM_STATE,
            eval_metric="logloss",
            scale_pos_weight=scale_pos_weight,
            n_estimators=200,
            max_depth=3,
            learning_rate=0.05,
            subsample=0.9,
            colsample_bytree=0.9,
        )
    else:
        print("⚠️ XGBoost is not installed. Skipping XGBoost model.")

    return models


def evaluate_predictions(y_true: pd.Series, preds: np.ndarray, probs: np.ndarray) -> Dict[str, float]:
    metrics = {
        "accuracy": accuracy_score(y_true, preds),
        "precision": precision_score(y_true, preds, zero_division=0),
        "recall": recall_score(y_true, preds, zero_division=0),
        "f1": f1_score(y_true, preds, zero_division=0),
        "auc": np.nan,
    }

    if len(np.unique(y_true)) == 2:
        metrics["auc"] = roc_auc_score(y_true, probs)

    return metrics


def fit_and_evaluate_models(
    X_train: pd.DataFrame,
    X_test: pd.DataFrame,
    y_train: pd.Series,
    y_test: pd.Series,
) -> Tuple[Dict[str, object], pd.DataFrame, Dict[str, Dict[str, np.ndarray]]]:
    models = get_models(y_train)
    fitted_models: Dict[str, object] = {}
    results = []
    predictions: Dict[str, Dict[str, np.ndarray]] = {}

    print("\n" + "=" * 80)
    print("🏆 Training and Comparing Models")
    print("=" * 80)

    for model_name, model in models.items():
        print(f"\n🔹 Training {model_name}...")
        model.fit(X_train, y_train)

        fitted_model = model.best_estimator_ if isinstance(model, GridSearchCV) else model
        fitted_models[model_name] = fitted_model

        preds = fitted_model.predict(X_test)
        probs = fitted_model.predict_proba(X_test)[:, 1]
        metrics = evaluate_predictions(y_test, preds, probs)

        if isinstance(model, GridSearchCV):
            print(f"   Best params: {model.best_params_}")

        print(
            f"   Accuracy: {metrics['accuracy']:.3f} | "
            f"Precision: {metrics['precision']:.3f} | "
            f"Recall: {metrics['recall']:.3f} | "
            f"F1: {metrics['f1']:.3f} | "
            f"AUC: {metrics['auc']:.3f}"
        )

        results.append({"model": model_name, **metrics})
        predictions[model_name] = {"preds": preds, "probs": probs}

    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values(
        by=["auc", "f1", "recall", "precision"],
        ascending=False,
        na_position="last",
    ).reset_index(drop=True)

    return fitted_models, results_df, predictions


def choose_best_model(results_df: pd.DataFrame) -> str:
    """
    No model is preferred in advance.
    The best model is selected by performance:
    1. AUC
    2. F1
    3. Recall
    4. Precision
    """
    best_model_name = str(results_df.iloc[0]["model"])

    print("\n" + "=" * 80)
    print("✅ Best Model Selected Automatically")
    print("=" * 80)
    print(results_df.round(4).to_string(index=False))
    print(f"\n🏅 Selected model for optimizer weights: {best_model_name}")

    return best_model_name


# =========================================================
# Weights extraction
# =========================================================
def normalize_weights(raw_values: np.ndarray) -> Dict[str, float]:
    raw_values = np.nan_to_num(np.abs(raw_values), nan=0.0)
    total = float(raw_values.sum())

    if total == 0:
        normalized = np.ones(len(FEATURE_NAMES)) / len(FEATURE_NAMES)
    else:
        normalized = raw_values / total

    return {name: round(float(value), 4) for name, value in zip(FEATURE_NAMES, normalized)}


def extract_model_weights(model_name: str, model: object) -> Dict[str, float]:
    if model_name == "Logistic Regression":
        raw_values = model.coef_[0]
    elif hasattr(model, "feature_importances_"):
        raw_values = model.feature_importances_
    else:
        raise ValueError(f"Cannot extract weights from model: {model_name}")

    return normalize_weights(raw_values)


def build_weights_comparison(fitted_models: Dict[str, object]) -> pd.DataFrame:
    comparison = {}

    for model_name, model in fitted_models.items():
        comparison[model_name] = extract_model_weights(model_name, model)

    weights_df = pd.DataFrame(comparison)
    weights_df.index.name = "criterion"
    return weights_df


# =========================================================
# Semi-supervised pseudo-labeling
# =========================================================
def create_pseudo_labeled_data(
    unlabeled_df: pd.DataFrame,
    base_model: object,
) -> pd.DataFrame:
    if unlabeled_df.empty:
        print("\n🟡 No unlabeled sessions found for semi-supervised learning")
        return pd.DataFrame(columns=unlabeled_df.columns)

    pseudo_groups = []
    checked_sessions = 0

    for session_id, group in unlabeled_df.groupby(SESSION_COLUMN):
        # Pseudo-labeling requires at least 2 alternatives to compare.
        # A single unselected schedule is kept as unlabeled and is not guessed.
        if len(group) < 2 or len(group) > 3:
            continue

        checked_sessions += 1
        group = group.copy()
        X_group = group[FEATURE_COLUMNS]
        probs = base_model.predict_proba(X_group)[:, 1]

        sorted_probs = np.sort(probs)[::-1]
        best_row_position = int(np.argmax(probs))
        best_prob = float(sorted_probs[0])
        second_best_prob = float(sorted_probs[1])
        margin = best_prob - second_best_prob

        if best_prob >= PSEUDO_LABEL_MIN_PROB and margin >= PSEUDO_LABEL_MIN_MARGIN:
            group["is_selected_clean"] = 0
            group.iloc[best_row_position, group.columns.get_loc("is_selected_clean")] = 1
            group["label_source"] = "pseudo"
            pseudo_groups.append(group)

    pseudo_df = pd.concat(pseudo_groups, ignore_index=True) if pseudo_groups else pd.DataFrame(columns=unlabeled_df.columns)

    print("\n" + "=" * 80)
    print("🧪 Semi-Supervised Pseudo Labeling")
    print("=" * 80)
    print(f"Unlabeled sessions checked: {checked_sessions}")
    print(f"Pseudo-labeled sessions accepted: {count_sessions(pseudo_df)}")
    print(f"Thresholds: probability >= {PSEUDO_LABEL_MIN_PROB}, margin >= {PSEUDO_LABEL_MIN_MARGIN}")

    return pseudo_df


# =========================================================
# DB save
# =========================================================
def safe_float(value) -> float | None:
    if value is None:
        return None
    value = float(value)
    if np.isnan(value) or np.isinf(value):
        return None
    return value


def save_weights_to_db(
    supabase: Client,
    best_model_name: str,
    best_metrics: pd.Series,
    weights: Dict[str, float],
    num_records: int,
    semi_supervised_used: bool,
) -> None:
    row = {
        "model_name": best_model_name,
        "weight_g": weights.get("G"),
        "weight_d": weights.get("D"),
        "weight_ld": weights.get("LD"),
        "weight_t": weights.get("T"),
        "weight_lw": weights.get("LW"),
        "weight_c": weights.get("C"),
        "weight_a": weights.get("A"),
        "num_training_records": int(num_records),
        "accuracy": safe_float(best_metrics["accuracy"]),
        "precision": safe_float(best_metrics["precision"]),
        "recall": safe_float(best_metrics["recall"]),
        "f1_score": safe_float(best_metrics["f1"]),
        "auc": safe_float(best_metrics["auc"]),
        "semi_supervised_used": bool(semi_supervised_used),
        "is_best_run": True,
    }

    # Only the newest run is the active one; optimizer weights are read from it.
    supabase.table(WEIGHTS_TABLE_NAME).update({"is_best_run": False}).eq("is_best_run", True).execute()
    supabase.table(WEIGHTS_TABLE_NAME).insert(row).execute()
    print(f"\n💾 Saved selected weights to Supabase table: {WEIGHTS_TABLE_NAME}")


# =========================================================
# Visualizations
# =========================================================
def maybe_save_plot(filename: str) -> None:
    if SAVE_PLOTS_TO_FILES:
        os.makedirs(PLOTS_OUTPUT_DIR, exist_ok=True)
        path = os.path.join(PLOTS_OUTPUT_DIR, filename)
        plt.savefig(path, dpi=200, bbox_inches="tight")
        print(f"   💾 Saved graph: {path}")


def finish_plot(filename: str) -> None:
    maybe_save_plot(filename)
    if SHOW_PLOTS:
        plt.show()
    else:
        plt.close()



def plot_correlation_matrix(valid_labeled_df: pd.DataFrame) -> None:
    """
    Creates a correlation table and heatmap between the schedule criteria
    and the user selection label.

    This is an exploratory analysis step:
    - It helps identify relationships between criteria.
    - It helps identify which criteria are directly correlated with user selection.
    - It does not replace the ML model weights.
    """
    correlation_columns = FEATURE_COLUMNS + ["is_selected_clean"]

    if valid_labeled_df.empty:
        print("\n🟡 Correlation matrix skipped because no valid labeled records exist")
        return

    corr_df = valid_labeled_df[correlation_columns].copy()

    rename_map = {
        "score_g": "G",
        "score_d": "D",
        "score_ld": "LD",
        "score_t": "T",
        "score_lw": "LW",
        "score_c": "C",
        "score_a": "A",
        "is_selected_clean": "Selected",
    }
    corr_df = corr_df.rename(columns=rename_map)

    correlation_matrix = corr_df.corr(method="pearson")

    print("\n" + "=" * 80)
    print("📌 Correlation Matrix: Criteria and User Selection")
    print("=" * 80)
    print(correlation_matrix.round(3).to_string())

    if SAVE_PLOTS_TO_FILES:
        os.makedirs(PLOTS_OUTPUT_DIR, exist_ok=True)
        csv_path = os.path.join(PLOTS_OUTPUT_DIR, "00_correlation_matrix.csv")
        correlation_matrix.round(4).to_csv(csv_path, encoding="utf-8-sig")
        print(f"   💾 Saved correlation table: {csv_path}")

    labels = list(correlation_matrix.columns)

    fig, ax = plt.subplots(figsize=(9, 7))
    im = ax.imshow(correlation_matrix.values, cmap="Blues", vmin=-1, vmax=1)

    ax.set_title("Correlation Matrix: Criteria and User Selection", fontsize=14, fontweight="bold")
    ax.set_xticks(np.arange(len(labels)))
    ax.set_yticks(np.arange(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_yticklabels(labels)

    # White grid lines for readability.
    ax.set_xticks(np.arange(-0.5, len(labels), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(labels), 1), minor=True)
    ax.grid(which="minor", color="white", linestyle="-", linewidth=1.5)
    ax.tick_params(which="minor", bottom=False, left=False)

    # Add values inside each cell.
    for i in range(len(labels)):
        for j in range(len(labels)):
            value = correlation_matrix.iloc[i, j]
            text_color = "white" if abs(value) > 0.55 else "black"
            ax.text(
                j,
                i,
                f"{value:.2f}",
                ha="center",
                va="center",
                color=text_color,
                fontsize=9,
                fontweight="bold" if abs(value) > 0.5 else "normal",
            )

    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Pearson correlation", rotation=270, labelpad=15)

    plt.tight_layout()
    finish_plot("00_correlation_matrix.png")


def plot_data_quality_summary(
    valid_labeled_df: pd.DataFrame,
    unlabeled_df: pd.DataFrame,
    invalid_df: pd.DataFrame,
    too_many_schedules_df: pd.DataFrame,
) -> None:
    """Shows which sessions were usable for training and which were not."""
    labels = [
        "Valid labeled\n(used for training)",
        "Unlabeled\n(no choice)",
        "Invalid\n>1 selected",
        "Invalid\n>3 schedules",
    ]
    values = [
        count_sessions(valid_labeled_df),
        count_sessions(unlabeled_df),
        count_sessions(invalid_df),
        count_sessions(too_many_schedules_df),
    ]

    plt.figure(figsize=(8, 5))
    bars = plt.bar(labels, values)
    plt.title("Data Quality Summary by Session")
    plt.xlabel("Session type")
    plt.ylabel("Number of sessions")

    for bar, value in zip(bars, values):
        plt.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            str(value),
            ha="center",
            va="bottom",
        )

    plt.tight_layout()
    finish_plot("01_data_quality_summary.png")


def plot_model_performance(results_df: pd.DataFrame, best_model_name: str) -> None:
    """Compares models by the main evaluation metrics."""
    metrics_to_plot = ["accuracy", "precision", "recall", "f1", "auc"]
    plot_df = results_df.set_index("model")[metrics_to_plot]

    ax = plot_df.plot(kind="bar", figsize=(10, 6))
    plt.title("Model Performance Comparison")
    plt.xlabel("Model")
    plt.ylabel("Metric score")
    plt.ylim(0, 1)
    plt.xticks(rotation=0)
    plt.legend(title="Metric")

    # Mark the selected model on the x-axis label area by adding a small note.
    plt.figtext(
        0.5,
        -0.02,
        f"Selected model for optimizer weights: {best_model_name}",
        ha="center",
        fontsize=10,
    )

    for container in ax.containers:
        ax.bar_label(container, fmt="%.2f", fontsize=8, padding=2)

    plt.tight_layout()
    finish_plot("02_model_performance_comparison.png")


def plot_weights_comparison(weights_df: pd.DataFrame) -> None:
    """Compares criterion weight / importance across all trained models."""
    ax = weights_df.plot(kind="bar", figsize=(14, 7), rot=0)

    plt.title("Criterion Weight / Importance Comparison by Model", fontsize=16, fontweight="bold")
    plt.xlabel("Criterion", fontsize=12)
    plt.ylabel("Normalized Weight / Importance", fontsize=12)
    plt.legend(title="Model")

    # Add numeric values above each bar.
    for container in ax.containers:
        ax.bar_label(container, fmt="%.3f", padding=3, fontsize=8)

    max_value = float(weights_df.max().max()) if not weights_df.empty else 0.25
    plt.ylim(0, max(0.25, max_value + 0.06))
    plt.tight_layout()
    finish_plot("03_criterion_importance_by_model.png")


def plot_final_optimizer_weights(best_weights: Dict[str, float], best_model_name: str) -> None:
    """Shows only the final weights that should be used by optimizer.py."""
    weights_series = pd.Series(best_weights).sort_values(ascending=False)

    plt.figure(figsize=(8, 5))
    bars = plt.bar(weights_series.index, weights_series.values)
    plt.title(f"Final Optimizer Weights - {best_model_name}")
    plt.xlabel("Criterion")
    plt.ylabel("Final normalized weight")
    plt.ylim(0, max(0.25, float(weights_series.max()) + 0.05))

    for bar, value in zip(bars, weights_series.values):
        plt.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{value:.3f}",
            ha="center",
            va="bottom",
        )

    plt.tight_layout()
    finish_plot("04_final_optimizer_weights.png")


def plot_roc_curves(y_test: pd.Series, predictions: Dict[str, Dict[str, np.ndarray]]) -> None:
    """Compares ROC curves for all models."""
    if len(np.unique(y_test)) != 2:
        print("   🟡 ROC curve skipped because y_test contains only one class")
        return

    plt.figure(figsize=(8, 6))
    for model_name, pred_data in predictions.items():
        probs = pred_data["probs"]
        fpr, tpr, _ = roc_curve(y_test, probs)
        auc_val = roc_auc_score(y_test, probs)
        plt.plot(fpr, tpr, label=f"{model_name} AUC={auc_val:.3f}")

    plt.plot([0, 1], [0, 1], linestyle="--")
    plt.title("ROC Curve Comparison")
    plt.xlabel("False Positive Rate")
    plt.ylabel("True Positive Rate")
    plt.legend()
    plt.tight_layout()
    finish_plot("05_roc_curve_comparison.png")


def plot_all_confusion_matrices(
    y_test: pd.Series,
    predictions: Dict[str, Dict[str, np.ndarray]],
    best_model_name: str,
) -> None:
    """Creates one clean confusion matrix for every trained model."""
    for model_name, pred_data in predictions.items():
        cm = confusion_matrix(y_test, pred_data["preds"], labels=[0, 1])

        fig, ax = plt.subplots(figsize=(6, 5))
        im = ax.imshow(cm, cmap="Blues")

        title = f"Confusion Matrix - {model_name}"
        if model_name == best_model_name:
            title += " (Selected Model)"

        ax.set_title(title, fontsize=14, fontweight="bold")
        ax.set_xlabel("Predicted", fontsize=12)
        ax.set_ylabel("Actual", fontsize=12)
        ax.set_xticks([0, 1])
        ax.set_yticks([0, 1])
        ax.set_xticklabels(["Rejected", "Selected"], fontsize=11)
        ax.set_yticklabels(["Rejected", "Selected"], fontsize=11)

        # White grid lines for a cleaner look.
        ax.set_xticks(np.arange(-0.5, 2, 1), minor=True)
        ax.set_yticks(np.arange(-0.5, 2, 1), minor=True)
        ax.grid(which="minor", color="white", linestyle="-", linewidth=2)
        ax.tick_params(which="minor", bottom=False, left=False)

        max_val = cm.max() if cm.size else 0
        for i in range(cm.shape[0]):
            for j in range(cm.shape[1]):
                text_color = "white" if cm[i, j] > max_val / 2 else "black"
                ax.text(
                    j,
                    i,
                    str(cm[i, j]),
                    ha="center",
                    va="center",
                    color=text_color,
                    fontsize=16,
                    fontweight="bold",
                )

        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        plt.tight_layout()

        safe_name = model_name.lower().replace(" ", "_").replace("-", "_")
        finish_plot(f"06_confusion_matrix_{safe_name}.png")


def plot_results(
    y_test: pd.Series,
    predictions: Dict[str, Dict[str, np.ndarray]],
    weights_df: pd.DataFrame,
    results_df: pd.DataFrame,
    best_model_name: str,
    best_weights: Dict[str, float],
    valid_labeled_df: pd.DataFrame,
    unlabeled_df: pd.DataFrame,
    invalid_df: pd.DataFrame,
    too_many_schedules_df: pd.DataFrame,
) -> None:
    print("\n🎨 Displaying and saving graphs...")

    plot_correlation_matrix(valid_labeled_df)

    plot_data_quality_summary(
        valid_labeled_df=valid_labeled_df,
        unlabeled_df=unlabeled_df,
        invalid_df=invalid_df,
        too_many_schedules_df=too_many_schedules_df,
    )
    plot_model_performance(results_df, best_model_name)
    plot_weights_comparison(weights_df)
    plot_final_optimizer_weights(best_weights, best_model_name)
    plot_roc_curves(y_test, predictions)
    plot_all_confusion_matrices(y_test, predictions, best_model_name)


# =========================================================
# Main pipeline
# =========================================================
def run_pipeline_on_df(
    df: pd.DataFrame,
    supabase: Client | None = None,
    save_to_db: bool = SAVE_WEIGHTS_TO_DB,
    make_plots: bool = True,
    plots_dir: str | None = None,
    show_plots: bool | None = None,
) -> Dict[str, object]:
    """Runs the full pipeline on a generated_schedules DataFrame and returns a run summary."""
    global PLOTS_OUTPUT_DIR, SHOW_PLOTS
    if plots_dir is not None:
        PLOTS_OUTPUT_DIR = plots_dir
    if show_plots is not None:
        SHOW_PLOTS = show_plots

    print(f"🔎 Using session column: {SESSION_COLUMN}")
    validate_required_columns(df)

    df = normalize_is_selected(df)
    df = prepare_features(df)

    valid_labeled_df, unlabeled_df, invalid_df, too_many_schedules_df = split_sessions_by_quality(df)

    if valid_labeled_df.empty:
        raise ValueError("No valid labeled sessions found. Need sessions with exactly one selected schedule.")

    selected_count = int(valid_labeled_df["is_selected_clean"].sum())
    if selected_count < MIN_SELECTED_SCHEDULES:
        raise ValueError(
            f"Not enough selected schedules for meaningful training. "
            f"Found {selected_count}, need at least {MIN_SELECTED_SCHEDULES}."
        )

    X_train, X_test, y_train, y_test = split_train_test_by_session(valid_labeled_df)

    # Stage 1: supervised training using only real labeled sessions
    fitted_models, results_df, predictions = fit_and_evaluate_models(X_train, X_test, y_train, y_test)
    best_model_name = choose_best_model(results_df)
    best_model = fitted_models[best_model_name]

    semi_supervised_used = False
    pseudo_sessions = 0
    final_training_df = valid_labeled_df.copy()

    # Stage 2: optional semi-supervised pseudo-labeling
    if USE_SEMI_SUPERVISED:
        pseudo_df = create_pseudo_labeled_data(unlabeled_df, best_model)

        if not pseudo_df.empty:
            semi_supervised_used = True
            pseudo_sessions = count_sessions(pseudo_df)
            final_training_df = pd.concat([valid_labeled_df, pseudo_df], ignore_index=True)
            print(f"\n🔁 Re-training models with real + pseudo labels: {len(final_training_df)} records")

            X_train, X_test, y_train, y_test = split_train_test_by_session(final_training_df)
            fitted_models, results_df, predictions = fit_and_evaluate_models(X_train, X_test, y_train, y_test)
            best_model_name = choose_best_model(results_df)
            best_model = fitted_models[best_model_name]
        else:
            print("🟡 Semi-supervised mode enabled, but no pseudo labels passed the confidence threshold")

    # Extract final weights
    weights_df = build_weights_comparison(fitted_models)
    best_weights = extract_model_weights(best_model_name, best_model)

    print("\n" + "=" * 80)
    print("⚖️ Weights / Feature Importance Comparison")
    print("=" * 80)
    print(weights_df.round(4).to_string())

    print("\n" + "=" * 80)
    print("✅ Final Weights for optimizer.py")
    print("=" * 80)
    print(f"Selected model: {best_model_name}")
    print({k: round(v, 4) for k, v in best_weights.items()})

    print("\nSuggested optimizer weights format:")
    print("CRITERION_WEIGHTS = {")
    for key, value in best_weights.items():
        print(f"    '{key}': {value},")
    print("}")

    best_metrics = results_df[results_df["model"] == best_model_name].iloc[0]
    if save_to_db and supabase is not None:
        save_weights_to_db(
            supabase=supabase,
            best_model_name=best_model_name,
            best_metrics=best_metrics,
            weights=best_weights,
            num_records=len(final_training_df),
            semi_supervised_used=semi_supervised_used,
        )
    else:
        print("\n🟡 Saving to DB is disabled for this run, so weights were not saved to Supabase")

    if make_plots:
        plot_results(
            y_test=y_test,
            predictions=predictions,
            weights_df=weights_df,
            results_df=results_df,
            best_model_name=best_model_name,
            best_weights=best_weights,
            valid_labeled_df=valid_labeled_df,
            unlabeled_df=unlabeled_df,
            invalid_df=invalid_df,
            too_many_schedules_df=too_many_schedules_df,
        )

    return {
        "labeled_sessions": count_sessions(valid_labeled_df),
        "unlabeled_sessions": count_sessions(unlabeled_df),
        "pseudo_labeled_sessions": pseudo_sessions,
        "num_training_records": len(final_training_df),
        "semi_supervised_used": semi_supervised_used,
        "best_model": best_model_name,
        "best_weights": best_weights,
        "models": results_df.round(4).to_dict(orient="records"),
        "weights_by_model": weights_df.round(4).to_dict(),
    }


def run_analytics_pipeline() -> None:
    supabase = get_supabase_client()
    df = fetch_schedules(supabase)
    run_pipeline_on_df(df, supabase=supabase)


# =========================================================
# Retraining every CHECKPOINT_STEP labeled sessions
# =========================================================
def session_label_table(df: pd.DataFrame) -> pd.DataFrame:
    """One row per session: when it was created and whether it is labeled (exactly one selection)."""
    df = normalize_is_selected(df)
    sessions = df.groupby(SESSION_COLUMN).agg(
        created_at=("created_at", "min"),
        selected=("is_selected_clean", "sum"),
        size=("is_selected_clean", "size"),
    ).reset_index()
    sessions["created_at"] = pd.to_datetime(sessions["created_at"], utc=True, format="mixed")
    sessions["is_labeled"] = (
        (sessions["selected"] == 1)
        & (sessions["size"] >= MIN_ALTERNATIVES_FOR_TRAINING)
        & (sessions["size"] <= 3)
    )
    return sessions.sort_values("created_at").reset_index(drop=True)


def run_checkpoints(df: pd.DataFrame, step: int = CHECKPOINT_STEP, output_dir: str = PLOTS_OUTPUT_DIR) -> List[Dict[str, object]]:
    """
    Replays history in creation order: every time `step` more labeled sessions exist,
    trains all models on the data available at that moment and keeps the best one.
    The last checkpoint also includes any remainder (e.g. 350 when step is 100).
    """
    sessions = session_label_table(df)
    labeled = sessions[sessions["is_labeled"]]
    total = len(labeled)
    targets = list(range(step, total + 1, step))
    if total and (not targets or targets[-1] != total):
        targets.append(total)

    runs = []
    for target in targets:
        cutoff = labeled.iloc[target - 1]["created_at"]
        visible_sessions = set(sessions[sessions["created_at"] <= cutoff][SESSION_COLUMN])
        checkpoint_df = df[df[SESSION_COLUMN].isin(visible_sessions)].copy()

        print("\n" + "#" * 80)
        print(f"📍 Checkpoint: {target} labeled sessions")
        print("#" * 80)
        summary = run_pipeline_on_df(
            checkpoint_df,
            save_to_db=False,
            make_plots=True,
            plots_dir=os.path.join(output_dir, f"checkpoint_{target}"),
            show_plots=False,
        )
        summary["checkpoint"] = target
        runs.append(summary)
    return runs


def get_last_run_time(supabase: Client):
    res = supabase.table(WEIGHTS_TABLE_NAME).select("created_at").order("created_at", desc=True).limit(1).execute()
    if not res.data:
        return None
    return pd.to_datetime(res.data[0]["created_at"], utc=True, format="mixed")


def maybe_retrain(
    supabase: Client | None = None,
    force: bool = False,
    step: int = CHECKPOINT_STEP,
    make_plots: bool = True,
) -> Dict[str, object]:
    """
    Live retraining hook (called after a user selects a schedule).
    Retrains when at least `step` labeled sessions were created since the last saved run.
    """
    supabase = supabase or get_supabase_client()
    df = fetch_schedules(supabase)
    sessions = session_label_table(df)
    labeled = sessions[sessions["is_labeled"]]

    last_run = get_last_run_time(supabase)
    new_labeled = int(len(labeled) if last_run is None else (labeled["created_at"] > last_run).sum())

    if not force and new_labeled < step:
        print(f"[ML] Retrain skipped: {new_labeled}/{step} new labeled sessions since last run")
        return {"trained": False, "new_labeled_sessions": new_labeled, "step": step}

    summary = run_pipeline_on_df(
        df,
        supabase=supabase,
        save_to_db=True,
        make_plots=make_plots,
        plots_dir=os.path.join("analytics_outputs", f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
        show_plots=False,
    )
    summary.update(trained=True, new_labeled_sessions=new_labeled)
    return summary


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--from-dir", help="read generated_schedules.json from this folder instead of Supabase")
    parser.add_argument("--checkpoints", action="store_true", help=f"train at every {CHECKPOINT_STEP} labeled sessions")
    parser.add_argument("--step", type=int, default=CHECKPOINT_STEP)
    args = parser.parse_args()

    if args.from_dir:
        with open(os.path.join(args.from_dir, "generated_schedules.json"), encoding="utf-8") as f:
            source_df = pd.DataFrame(json.load(f))
    else:
        source_df = None

    if args.checkpoints:
        if source_df is None:
            source_df = fetch_schedules(get_supabase_client())
        out_dir = os.path.join(args.from_dir or ".", PLOTS_OUTPUT_DIR)
        all_runs = run_checkpoints(source_df, step=args.step, output_dir=out_dir)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "checkpoint_runs.json"), "w", encoding="utf-8") as f:
            json.dump(all_runs, f, ensure_ascii=False, indent=1, default=str)
        print("\n" + "=" * 80)
        print("📈 Checkpoint summary")
        print("=" * 80)
        for run in all_runs:
            best = next(m for m in run["models"] if m["model"] == run["best_model"])
            print(f"{run['checkpoint']:>4} labeled | best: {run['best_model']:<20} AUC={best['auc']} F1={best['f1']} | weights={run['best_weights']}")
    elif source_df is not None:
        run_pipeline_on_df(source_df, save_to_db=False)
    else:
        run_analytics_pipeline()
