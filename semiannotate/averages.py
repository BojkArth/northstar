# vim: fdm=indent
# author:     Fabio Zanini
# date:       17/06/19
# content:    SemiAnnotate with atlas averages
__all__ = ['SemiAnnotate']


import numpy as np
import pandas as pd
from .fetch_atlas import AtlasFetcher

try:
    from anndata import AnnData
except ImportError:
    AnnData = None


class Averages(object):
    '''Annotate new cell types using averages of an atlas'''

    def __init__(
            self,
            atlas,
            new_data,
            n_cells_per_type=None,
            n_features_per_cell_type=30,
            n_features_overdispersed=500,
            n_pcs=20,
            n_neighbors=10,
            n_neighbors_out_of_atlas=5,
            distance_metric='correlation',
            threshold_neighborhood=0.8,
            clustering_metric='cpm',
            resolution_parameter=0.001,
            normalize_counts=True,
            ):
        '''Prepare the model for cell annotation

        Args:
            atlas (str, list of str, or dict): cell atlas to use. If a str,
            the corresponding cell atlas from:

            https://github.com/iosonofabio/atlas_averages/blob/master/table.tsv

            is fetched (check the first column for atlas names). If a list of
            str, multiple atlases will be fetched and combined. Only features
            that are in all atlases will be kept. If you use this feature, be
            careful to not mix atlases from different species. If a dict, it
            describes a custom cell atlas and must have two entries.
            'number_of_cells' is a pandas Series with the cell types as
            index and the number of cells to use for each cell type as values.
            'counts' is a pandas.DataFrame or an anndata.AnnData structure.
            If a DataFrame, it must have features as rows and cell types as
            columns; if an AnnData, it is reversed (AnnData uses a
            different convention) and it must have the cell types as rows
            (obs_names) and the features as columns (var_names). If an AnnData,
            it will be converted into a DataFrame.

            new_data (pandas.DataFrame or anndata.AnnData): the new data to be
            clustered. If a dataframe, t must have features as rows and
            cell names as columns (as in loom files). anndata uses the opposite
            convention, so it must have cell names as rows (obs_names) and
            features as columns (var_names) and this class will transpose it.

            n_cells_per_type (None or int): if None, use the number of cells
            per type from the atlas. Else, fix it to this number for all types.

            n_features_per_cell_type (int): number of features marking each
            fixed column (atlas cell type).

            n_features_overdispersed (int): number of unbiased, overdispersed
            features to be picked from the new dataset.

            n_pcs (int): number of principal components to keep in the weighted
            PCA.

            n_neighbors (int): number of neighbors in the similarity graph.

            n_neighbors_out_of_atlas (int): number of neighbors coming out of
            the atlas nodes into the new dataset.

            distance_metric (str or function): metric to use as distance. If a
            string, it should be a metric accepted by scipy.spatial.distance.cdist.
            If a function, it should accept a (M x N)  and a (M x N1) data matrices
            as input and return a (N x N1) distance matrix. N includes both the
            fixed and the free columns, whereas N1 = N - n_fixed only includes the
            free columns.

            threshold_neighborhood (float): do not consider distances larger than this as
            neighbors

            clustering_metric (str): 'cpm' (default, Cell Potts Model) or
            'modularity'. Sets the type of partition used in the clustering
            step.

            resolution_parameter (float): number between 0 and 1 that sets
            how easy it is for the clustering algorithm to make new clusters

            normalize_counts (bool): whether to renormalize the counts at the
            merging stage to make sure atlas and new data follow the same
            normalization. Be careful if you turn this off.
        '''

        self.atlas = atlas
        self.new_data = new_data
        self.n_cells_per_type = n_cells_per_type
        self.n_features_per_cell_type = n_features_per_cell_type
        self.n_features_overdispersed = n_features_overdispersed
        self.n_pcs = n_pcs
        self.n_neighbors = n_neighbors
        self.n_neighbors_out_of_atlas = n_neighbors_out_of_atlas
        self.distance_metric = distance_metric
        self.threshold_neighborhood = threshold_neighborhood
        self.clustering_metric = clustering_metric
        self.resolution_parameter = resolution_parameter
        self.normalize_counts = normalize_counts

    def _check_init_arguments(self):
        # Custom atlas
        at = self.atlas
        if not np.isscalar(at):
            if not isinstance(self.atlas, dict):
                raise ValueError('atlas must be a dict')
            if 'counts' not in at:
                raise ValueError('atlas must have a "counts" key')
            if 'number_of_cells' not in at:
                raise ValueError('atlas must have a "number_of_cells" key')

            # The counts can be pandas.DataFrame or anndata.AnnData
            if not isinstance(at['counts'], pd.DataFrame):
                if AnnData is None:
                    raise ValueError('atlas["counts"] must be a DataFrame')
                elif not isinstance(at['counts'], AnnData):
                    raise ValueError('atlas["counts"] must be a DataFrame'
                                     ' or AnnData object')

                # AnnData uses features as columns, to transpose and convert
                at['counts'] = at['counts'].T.to_df()

            # even within AnnData, metadata colunms are pandas.DataFrame
            if not isinstance(at['number_of_cells'], pd.DataFrame):
                raise ValueError('atlas["number_of_cells"] must be a dataframe')
            if at['counts'].shape[1] != at['number_of_cells'].shape[0]:
                raise ValueError(
                    'atlas counts and number_of_cells must have the same cells')
            if (at['counts'].columns != at['number_of_cells'].index).any():
                raise ValueError(
                    'atlas counts and number_of_cells must have the same cells')

        # Make sure new data is a dataframe
        nd = self.new_data
        if not isinstance(nd, pd.DataFrame):
            if AnnData is None:
                raise ValueError('new data must be a DataFrame')
            elif not isinstance(nd, AnnData):
                raise ValueError('new_data must be a DataFrame'
                                 ' or AnnData object')

            # AnnData uses features as columns, to transpose and convert
            self.new_data = nd = nd.T.to_df()

        nf1 = self.n_features_per_cell_type
        if not isinstance(nf1, int):
            raise ValueError('n_features_per_cell_type must be an int >= 0')
        nf2 = self.n_features_overdispersed
        if not isinstance(nf1, int):
            raise ValueError('n_features_overdispersed must be an int >= 0')
        if (nf1 < 1) and (nf2 < 1):
            raise ValueError('No features selected')

    def fetch_atlas_if_needed(self):
        '''Fetch atlas(es) if needed'''

        if np.isscalar(self.atlas):
            self.atlas = AtlasFetcher().fetch_atlas(self.atlas)
        elif isinstance(self.atlas, list) or isinstance(self.atlas, tuple):
            self.atlas = AtlasFetcher().fetch_multiple_atlases(self.atlas)

    def merge_atlas_newdata(self):
        '''Merge the averaged atlas data and the new data

        This function sets the properties:
            - n_fixed: the number of cell types in the atlas
            - n_free: the number of cell types in the new data
            - cell_types: a 1D array with the cell types of the atlas
            - features_all: a 1D array with the features that were found in
            - matrix: a 2D array with the merged counts
            - sizes: a 1D array with the sizes of each column in the matrix
            both the atlas and the new dataset.

        NOTE: is self.normalize is True, the merged count matrix is normalized
        by 1 million total counts.
        '''

        # Intersect features
        atlas_features = self.atlas['counts'].index.values
        new_data_features = self.new_data.index.values
        features = np.intersect1d(atlas_features, new_data_features)
        self.features_all = features

        # Cells types
        self.n_fixed = n_fixed = self.atlas['counts'].shape[1]
        self.n_free = n_free = self.new_data.shape[1]
        self.cell_types = self.atlas['counts'].columns.values

        # Count matrix
        L = len(features)
        N = n_fixed + n_free
        matrix = np.empty((L, N), dtype=np.float32)
        matrix[:, :n_fixed] = self.atlas['counts'].loc[features].values
        matrix[:, n_fixed:] = self.new_data.loc[features].values
        if self.normalize_counts:
            matrix *= 1e6 / matrix.sum(axis=0)
        self.matrix = matrix

        # Cell numbers
        self.sizes = np.ones(N, np.float32)
        if self.n_cells_per_type is not None:
            self.sizes[:self.n_fixed] *= self.n_cells_per_type
        else:
            self.sizes[:self.n_fixed] = self.atlas['number_of_cells'].values.astype(np.float32)

    def select_features(self):
        '''Select features that define heterogeneity of the atlas and new data

        Returns:
            ndarray of feature names.
        '''
        # Shorten arg names
        matrix = self.matrix
        n_fixed = self.n_fixed
        nf1 = self.n_features_per_cell_type
        nf2 = self.n_features_overdispersed

        features = set()

        # Atlas markers
        if (nf1 > 0) and (n_fixed > 1):
            for icol in range(n_fixed):
                ge1 = matrix[:, icol]
                ge2 = (matrix[:, :n_fixed].sum(axis=1) - ge1) / (n_fixed - 1)
                fold_change = np.log2(ge1 + 0.1) - np.log2(ge2 + 0.1)
                markers = np.argpartition(fold_change, -nf1)[-nf1:]
                features |= set(markers)

        # Unbiased on new data
        if nf2 > 0:
            nd_mean = matrix[:, n_fixed:].mean(axis=1)
            nd_var = matrix[:, n_fixed:].var(axis=1)
            fano = (nd_var + 1e-10) / (nd_mean + 1e-10)
            overdispersed = np.argpartition(fano, -nf2)[-nf2:]
            features |= set(overdispersed)

        features = list(features)

        self.features_selected = features
        self.matrix = self.matrix[features]

    def compute_neighbors(self):
        '''Compute k nearest neighbors from a matrix with fixed nodes

        Returns:
            list of lists with the first k or less indices of the neighbors for
            each free column. The length is N - n_fixed. For each now, there are
            less than k entries if no other column within the distance threshold
            were found, or if N < k.


        The algorithm proceeds as follows:
        0. take the log of the counts
        1. subtract the mean along the observation axis (N) and divide by the
        standard dev along the same axis
        2. calculate the weighted covariance matrix
        3. calculate normal PCA on that matrix
        4. calculate the distance matrix by expanding atlas columns
        5. calculate the k neighbors from the distance matrix, checking for
        threshold
        '''
        from scipy.spatial.distance import cdist

        matrix = self.matrix
        sizes = self.sizes
        n_fixed = self.n_fixed
        k = self.n_neighbors
        kout = self.n_neighbors_out_of_atlas
        n_pcs = self.n_pcs
        metric = self.distance_metric
        threshold = self.threshold_neighborhood

        # Test input arguments
        L, N = matrix.shape
        if len(sizes) != N:
            raise ValueError('Matrix and sizes dimensions do not match')
        if n_fixed >= N:
            raise ValueError('n_fixed larger or equal matrix number of columns')
        if n_pcs > min(L, N):
            raise ValueError('n_pcs greater than smaller matrix dimension, those eigenvalues are zero')

        # 0. take log
        matrix = np.log10(matrix + 0.1)

        # 1. standardize
        weights = 1.0 * sizes / sizes.sum()
        mean_w = matrix @ weights
        var_w = ((matrix.T - mean_w)**2).T @ weights
        std_w = np.sqrt(var_w)
        Xnorm = ((matrix.T - mean_w) / std_w).T

        # take care of non-varying components
        Xnorm[np.isnan(Xnorm)] = 0

        # 2. weighted covariance
        # This matrix has size L x L. Typically L ~ 500 << N, so the covariance
        # L x L is much smaller than N x N, hence it's fine
        cov_w = np.cov(Xnorm, fweights=sizes)

        # 3. PCA
        # rvects columns are the right singular vectors
        evals, evects = np.linalg.eig(cov_w)
        # sort by decreasing eigenvalue (explained variance) and truncate
        ind = evals.argsort()[::-1][:n_pcs]
        # NOTE: we do not actually need the eigenvalues anymore
        lvects = evects.T[ind]

        # calculate right singular vectors given the left singular vectors
        # NOTE: this is true even if we truncated the PCA via n_pcs << L
        # rvects columns are the right singular vectors
        rvects = (lvects @ Xnorm).T

        # 4. expand embedded vectors to account for sizes
        # NOTE: this could be done by carefully tracking multiplicities
        # in the neighborhood calculation, but it's not worth it: the
        # amount of overhead memory used here is small because only a few
        # principal components are used
        Ne = int(np.sum(sizes))
        rvectse = np.empty((Ne, n_pcs), np.float32)
        sizese = np.empty(Ne, int)
        i = 0
        for isi, size in enumerate(sizes):
            for j in range(int(size)):
                rvectse[i] = rvects[isi]
                sizese[i] = size
                i += 1

        # 5. calculate distance matrix and neighbors
        # we do it row by row, it costs a bit in terms of runtime but
        # has huge savings in terms of memory since we don't need the square
        # distance matrix
        n_fixede = int(np.sum(sizes[:n_fixed]))
        neighbors = []
        for i in range(Ne):
            drow = cdist(rvectse[[i]], rvectse, metric=metric)[0]

            # set distance to self as a high number, to avoid self
            drow[i] = drow.max() + 1

            # Find largest k negative distances (k neighbors)
            if i < n_fixede:
                ki = int(sizese[i] - 1) + kout
            else:
                ki = k

            ind = np.argpartition(-drow, -ki)[-ki:]

            # Discard the ones beyond threshold
            ind = ind[drow[ind] <= threshold]

            # Indices are not sorted within ind, so we need to sort them
            # in descending order by distance (more efficient in the next step)
            ind = ind[np.argsort(drow[ind])][::-1]

            neighbors.append(list(ind))

        self.neighbors = neighbors

    def compute_communities(self):
        '''Compute communities from a matrix with fixed nodes

        Returns:
            None, but SemiAnnotate.membership is set as an array with
            size N - n_fixed with the atlas cell types of all cells from the
            new dataset.
        '''
        import inspect
        import igraph as ig
        import leidenalg

        # Check whether this version of Leiden has fixed nodes support
        opt = leidenalg.Optimiser()
        sig = inspect.getfullargspec(opt.optimise_partition)
        if 'fixed_nodes' not in sig.args:
            raise ImportError('This version of the leidenalg module does not support fixed nodes. Please update to a later (development) version')

        matrix = self.matrix
        sizes = self.sizes
        n_fixed = self.n_fixed
        clustering_metric = self.clustering_metric
        resolution_parameter = self.resolution_parameter
        neighbors = self.neighbors

        L, N = matrix.shape
        n_fixede = int(np.sum(sizes[:n_fixed]))
        Ne = int(np.sum(sizes))

        # Construct graph from the lists of neighbors
        edges_d = set()
        for i, neis in enumerate(neighbors):
            for n in neis:
                edges_d.add(frozenset((i, n)))

        edges = [tuple(e) for e in edges_d]
        g = ig.Graph(n=N, edges=edges, directed=False)

        # NOTE: initial membership is singletons except for atlas nodes, which
        # get the membership they have.
        initial_membership = []
        for isi in range(N):
            if isi < n_fixed:
                for ii in range(int(self.sizes[isi])):
                    initial_membership.append(isi)
            else:
                initial_membership.append(isi)

        if len(initial_membership) != Ne:
            raise ValueError('initial_membership list has wrong length!')

        # Compute communities with semi-supervised Leiden
        if clustering_metric == 'cpm':
            partition = leidenalg.CPMVertexPartition(
                    g,
                    resolution_parameter=resolution_parameter,
                    initial_membership=initial_membership,
                    )
        elif clustering_metric == 'modularity':
            partition = leidenalg.ModularityVertexPartition(
                    g,
                    resolution_parameter=resolution_parameter,
                    initial_membership=initial_membership,
                    )
        else:
            raise ValueError(
                'clustering_metric not understood: {:}'.format(clustering_metric))

        fixed_nodes = [int(i < n_fixede) for i in range(Ne)]
        opt.optimise_partition(partition, fixed_nodes=fixed_nodes)
        membership = partition.membership[n_fixede:]

        # Convert the known cell types
        lstring = len(max(self.cell_types, key=len))
        self.membership = np.array(
                [str(x) for x in membership],
                dtype='U{:}'.format(lstring))
        for i, ct in enumerate(self.cell_types):
            self.membership[self.membership == str(i)] = ct

    def estimate_closest_atlas_cell_type(self):
        '''Estimate atlas cell type closest to each new cluster'''
        from scipy.spatial.distance import cdist

        matrix = self.matrix
        n_fixed = self.n_fixed
        metric = self.distance_metric
        cell_types = self.cell_types

        # Calculate averages for the new clusters
        ct_new = list(set(self.membership) - set(cell_types))
        N = len(ct_new)
        L = matrix.shape[0]
        avg_new = np.empty((L, N), np.float32)
        for i, ct in enumerate(ct_new):
            avg_new[i] = self.matrix[:, self.membership == ct].mean(axis=1)

        avg_atl = matrix[:, :n_fixed]

        # Calculate distance matrix between new and old in the high-dimensional
        # feature-selected space
        dmat = cdist(avg_new, avg_atl, metric=metric)

        # Pick the closest
        closest = np.argmin(dmat, axis=1)

        # Give it actual names
        closest = pd.Series(cell_types[closest], index=ct_new)

        return closest

    def __call__(
            self,
            select_features=True,
            ):
        '''Run SemiAnnotate with averages of the atlas

        Args:
            select_features (bool): Whether to select features or to use the
            full data matrix. The latter is useful if a different feature
            selection was performed outside of SemiAnnotate.

        Returns:
            None, but this instance of SemiAnnotate acquired the property
            `membership` containing the cluster memberships (cell types) of the
            columns except the first n_fixed. The first n_fixed columns are
            assumes to have distinct memberships in the range [0, n_fixed - 1].
        '''
        self._check_init_arguments()

        self.fetch_atlas_if_needed()

        self.merge_atlas_newdata()

        if select_features:
            self.select_features()

        self.compute_neighbors()

        self.compute_communities()
