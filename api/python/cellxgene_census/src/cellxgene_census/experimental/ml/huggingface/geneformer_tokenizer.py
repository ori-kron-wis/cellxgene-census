import pickle
from typing import Any, Dict, List, Optional, Sequence, Set

import numpy as np
import numpy.typing as npt
import scipy
import tiledbsoma

from .cell_dataset_builder import CellDatasetBuilder


class GeneformerTokenizer(CellDatasetBuilder):
    """Generate a Hugging Face `Dataset` containing Geneformer token sequences for each
    cell in CELLxGENE Census ExperimentAxisQuery results (human).

    This class requires the Geneformer package to be installed separately with:
    `pip install git+https://huggingface.co/ctheodoris/Geneformer@8df5dc1`

    Example usage:

    ```
    import cellxgene_census
    import tiledbsoma
    from cellxgene_census.experimental.ml.huggingface import GeneformerTokenizer

    with cellxgene_census.open_soma(census_version="latest") as census:
        with GeneformerTokenizer(
            census["census_data"]["homo_sapiens"],
            # set obs_query to define some subset of Census cells:
            obs_query=tiledbsoma.AxisQuery(value_filter="is_primary_data == True and tissue_general == 'tongue'"),
            obs_column_names=(
                "soma_joinid",
                "cell_type_ontology_term_id",
            ),
        ) as tokenizer:
            dataset = tokenizer.build()
    ```

    Dataset item contents:
    - `input_ids`: Geneformer token sequence for the cell
    - `length`: Length of the token sequence
    - and the specified `obs_column_names` (cell metadata from the experiment obs dataframe)
    """

    obs_column_names: Set[str]
    max_input_tokens: int
    special_token: bool

    # for each Geneformer-modeled gene that matches to at least one Census gene, the list of
    # matching Census gene/var soma_joinids. (one Geneformer-modeled gene may match several
    # Census genes based on gene_mapping_file.)
    model_gene_ids: List[List[int]]
    model_gene_tokens: npt.NDArray[np.int64]  # Geneformer token number for each model_gene_id
    model_gene_medians: npt.NDArray[np.float64]  # float for each model_gene_id
    model_cls_token: Optional[np.int64] = None
    model_sep_token: Optional[np.int64] = None

    def __init__(
        self,
        experiment: tiledbsoma.Experiment,
        *,
        obs_column_names: Optional[Sequence[str]] = None,
        obs_attributes: Optional[Sequence[str]] = None,
        max_input_tokens: int = 2048,
        special_token: bool = False,
        token_dictionary_file: str = "",
        gene_median_file: str = "",
        gene_mapping_file: str = "",
        **kwargs: Any,
    ) -> None:
        """- `experiment`: Census Experiment to query
        - `obs_query`: obs AxisQuery defining the set of Census cells to process (default all)
        - `obs_column_names`: obs dataframe columns (cell metadata) to propagate into attributes
           of each Dataset item
        - `max_input_tokens`: maximum length of Geneformer input token sequence (default 2048)
        - `special_token`: whether to affix separator tokens to the sequence (default False)
        - `token_dictionary_file`, `gene_median_file`: pickle files supplying the mapping of
          Ensembl human gene IDs onto Geneformer token numbers and median expression values.
          By default, these will be loaded from the Geneformer package.
        - `gene_mapping_file`: optional pickle file with mapping for Census gene IDs to model's
        """
        if obs_attributes:  # old name of obs_column_names
            obs_column_names = obs_attributes

        self.max_input_tokens = max_input_tokens
        self.special_token = special_token
        self.obs_column_names = set(obs_column_names) if obs_column_names else set()
        self._load_geneformer_data(experiment, token_dictionary_file, gene_median_file, gene_mapping_file)
        super().__init__(
            experiment,
            measurement_name="RNA",
            layer_name="raw",
            **kwargs,
        )

    def _load_geneformer_data(
        self,
        experiment: tiledbsoma.Experiment,
        token_dictionary_file: str,
        gene_median_file: str,
        gene_mapping_file: str,
    ) -> None:
        """Load (1) the experiment's genes dataframe and (2) Geneformer's static data
        files for gene tokens and median expression; then, intersect them to compute
        self.model_gene_{ids,tokens,medians}.
        """
        # TODO: this work could be reused for all queries on this experiment

        genes_df = (
            experiment.ms["RNA"]
            .var.read(column_names=["soma_joinid", "feature_id"])
            .concat()
            .to_pandas()
            .set_index("soma_joinid")
        )

        if not (token_dictionary_file and gene_median_file):
            try:
                import geneformer
            except ImportError:
                # pyproject.toml can't express Geneformer git+https dependency
                raise ImportError(
                    "Please install Geneformer with: "
                    "pip install git+https://huggingface.co/ctheodoris/Geneformer@8df5dc1"
                ) from None
            if not token_dictionary_file:
                token_dictionary_file = geneformer.tokenizer.TOKEN_DICTIONARY_FILE
            if not gene_median_file:
                gene_median_file = geneformer.tokenizer.GENE_MEDIAN_FILE
        with open(token_dictionary_file, "rb") as f:
            gene_token_dict = pickle.load(f)
        with open(gene_median_file, "rb") as f:
            gene_median_dict = pickle.load(f)

        gene_mapping = None
        if gene_mapping_file:
            with open(gene_mapping_file, "rb") as f:
                gene_mapping = pickle.load(f)

        # compute model_gene_{ids,tokens,medians} by joining genes_df with Geneformer's
        # dicts
        model_gene_id_by_ensg: Dict[str, int] = {}
        model_gene_ids: List[List[int]] = []
        model_gene_tokens: List[np.int64] = []
        model_gene_medians: List[np.float64] = []
        for gene_id, row in genes_df.iterrows():
            ensg = row["feature_id"]  # ENSG... gene id, which keys Geneformer's dicts
            if gene_mapping is not None:
                ensg = gene_mapping.get(ensg, ensg)
            if ensg in gene_token_dict:
                if ensg not in model_gene_id_by_ensg:
                    model_gene_id_by_ensg[ensg] = len(model_gene_ids)
                    model_gene_ids.append([])
                    model_gene_tokens.append(gene_token_dict[ensg])
                    model_gene_medians.append(gene_median_dict[ensg])
                model_gene_ids[model_gene_id_by_ensg[ensg]].append(gene_id)

        self.model_gene_ids = model_gene_ids
        self.model_gene_tokens = np.array(model_gene_tokens, dtype=np.int64)
        self.model_gene_medians = np.array(model_gene_medians, dtype=np.float64)

        assert len(self.model_gene_ids) == len(self.model_gene_tokens)
        assert len(self.model_gene_ids) == len(self.model_gene_medians)
        assert len(np.unique(self.model_gene_tokens)) == len(self.model_gene_tokens)
        assert np.all(self.model_gene_medians > 0)
        # Geneformer models protein-coding and miRNA genes, so the intersection should
        # be somewhere a little north of 20K.
        assert (
            len(self.model_gene_ids) > 20_000
        ), f"Mismatch between Census gene IDs and Geneformer token mappings (only {len(self.model_gene_ids)} common genes)"

        # Precompute a vector by which we'll multiply each cell's expression vector.
        # The denominator normalizes by Geneformer's median expression values.
        # The numerator 10K factor follows Geneformer's tokenizer; theoretically it doesn't affect
        # affect the rank order, but is probably intended to help with numerical precision.
        self.model_gene_medians_factor = 10_000.0 / self.model_gene_medians

        if self.special_token:
            self.model_cls_token = gene_token_dict["<cls>"]
            self.model_sep_token = gene_token_dict["<sep>"]

    def __enter__(self) -> "GeneformerTokenizer":
        super().__enter__()
        # On context entry, load the necessary cell metadata (obs_df)
        obs_column_names = list(self.obs_column_names)
        if "soma_joinid" not in self.obs_column_names:
            obs_column_names.append("soma_joinid")
        self.obs_df = self.obs(column_names=obs_column_names).concat().to_pandas().set_index("soma_joinid")
        return self

    def _map_block(
        self, block_cell_joinids: npt.NDArray[np.int64], Xblock: scipy.sparse.csr_matrix
    ) -> scipy.sparse.csr_matrix:
        model_gene_counts = [
            scipy.sparse.csc_matrix(Xblock[:, cols].sum(axis=1)) if len(cols) > 1 else Xblock[:, cols[0]].tocsc()
            for cols in self.model_gene_ids
        ]
        return scipy.sparse.hstack(model_gene_counts, format="csr")

    def cell_item(self, cell_joinid: int, cell_Xrow: scipy.sparse.csr_matrix) -> Dict[str, Any]:
        """Given the expression vector for one cell, compute the Dataset item providing
        the Geneformer inputs (token sequence and metadata).
        """
        # project cell_Xrow onto model_gene_ids and normalize with row sum & gene medians
        # notice we divide by the total count of the complete row (not only of the projected
        # values); this follows Geneformer's internal tokenizer.
        assert cell_Xrow.shape == (1, len(self.model_gene_ids))
        model_expr = cell_Xrow.multiply(self.model_gene_medians_factor / cell_Xrow.sum())
        assert isinstance(model_expr, scipy.sparse.coo_matrix)
        assert model_expr.shape == (1, len(self.model_gene_ids))

        # figure the resulting tokens in descending order of model_expr
        # (use sparse model_expr.{col,data} to naturally exclude undetected genes)
        token_order = model_expr.col[np.argsort(-model_expr.data)[: self.max_input_tokens]]
        input_ids = self.model_gene_tokens[token_order]

        if self.special_token:
            # affix special tokens, dropping the last two gene tokens if necessary
            if len(input_ids) == self.max_input_tokens:
                input_ids = input_ids[:-1]
            assert self.model_cls_token is not None
            input_ids = np.insert(input_ids, 0, self.model_cls_token)
            if len(input_ids) == self.max_input_tokens:
                input_ids = input_ids[:-1]
            assert self.model_sep_token is not None
            input_ids = np.append(input_ids, self.model_sep_token)

        ans = {"input_ids": input_ids, "length": len(input_ids)}
        # add the requested obs attributes
        for attr in self.obs_column_names:
            if attr != "soma_joinid":
                ans[attr] = self.obs_df.at[cell_joinid, attr]
            else:
                ans["soma_joinid"] = cell_joinid
        return ans
