# vim: fdm=indent
# author:     Fabio Zanini
# date:       17/06/19
# content:    semiannotate entry point for import
from .averages import Averages
from .subsample import Subsample
from .fetch_atlas import AtlasFetcher
from .version import version


__all__ = ['Aveages', 'Subsample', 'AtlasFetcher', 'version']
