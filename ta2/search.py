import itertools
import json
import logging
import os
import random
import signal
import warnings
from collections import defaultdict
from datetime import datetime, timedelta
from enum import Enum
from multiprocessing import Manager, Process

import numpy as np
from d3m.container.dataset import Dataset
from d3m.metadata.base import ArgumentType, Context
from d3m.metadata.pipeline import Pipeline, PrimitiveStep
from d3m.metadata.problem import TaskType
from d3m.runtime import DEFAULT_SCORING_PIPELINE_PATH
from d3m.runtime import evaluate as d3m_evaluate
from datamart import DatamartQuery
from datamart_rest import RESTDatamart

from ta2.tuning import SelectorTuner
from ta2.utils import dump_pipeline

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PIPELINES_DIR = os.path.join(BASE_DIR, 'pipelines')
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
FALLBACK_PIPELINE = 'fallback_pipeline.yml'

DATAMART_URL = os.getenv('DATAMART_URL_NYU', 'https://datamart.d3m.vida-nyu.org')

TUNING_PARAMETER = 'https://metadata.datadrivendiscovery.org/types/TuningParameter'

SUBPROCESS_PRIMITIVES = [
    'd3m.primitives.natural_language_processing.lda.Fastlvm'
]

LOGGER = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=DeprecationWarning)


class Templates(Enum):
    # SINGLE TABLE CLASSIFICATION
    SINGLE_TABLE_CLASSIFICATION_ENC_XGB = 'single_table_classification_encoding_xgb.yml'
    SINGLE_TABLE_CLASSIFICATION_AR_RF = 'single_table_classification_autorpi_rf.yml'
    SINGLE_TABLE_CLASSIFICATION_DFS_ROBUST_XGB = 'single_table_classification_dfs_robust_xgb.yml'
    # SINGLE_TABLE_CLASSIFICATION_DFS_XGB = 'single_table_classification_dfs_xgb.yml'
    # SINGLE_TABLE_CLASSIFICATION_GB = 'single_table_classification_gradient_boosting.yml'

    # SINGLE TABLE REGRESSION
    SINGLE_TABLE_REGRESSION_XGB = 'single_table_regression_xgb.yml'
    SINGLE_TABLE_REGRESSION_SC_XGB = 'single_table_regression_scale_xgb.yml'
    SINGLE_TABLE_REGRESSION_ENC_XGB = 'single_table_regression_encoding_xgb.yml'
    # SINGLE_TABLE_REGRESSION_DFS_XGB = 'single_table_regression_dfs_xgb.yml'
    # SINGLE_TABLE_REGRESSION_GB = 'single_table_regression_gradient_boosting.yml'

    # MISC
    SINGLE_TABLE_SEMI_CLASSIFICATION = 'single_table_semi_classification_autonbox.yml'
    SINGLE_TABLE_CLUSTERING = 'single_table_clustering_ekss.yml'

    # MULTI TABLE
    MULTI_TABLE_CLASSIFICATION_DFS_XGB = 'multi_table_classification_dfs_xgb.yml'
    MULTI_TABLE_CLASSIFICATION_LDA_LOGREG = 'multi_table_classification_lda_logreg.yml'
    MULTI_TABLE_REGRESSION = 'multi_table_regression_dfs_xgb.yml'

    # TIMESERIES CLASSIFICATION
    TIMESERIES_CLASSIFICATION_KN = 'time_series_classification_k_neighbors_kn.yml'
    TIMESERIES_CLASSIFICATION_DSBOX_LR = 'time_series_classification_dsbox_lr.yml'
    TIMESERIES_CLASSIFICATION_LSTM_FCN = 'time_series_classification_lstm_fcn.yml'
    # TIMESERIES_CLASSIFICATION_XGB = 'time_series_classification_xgb.yml'
    # TIMESERIES_CLASSIFICATION_RF = 'time_series_classification_rf.yml'

    # IMAGE
    IMAGE_REGRESSION = 'image_regression_resnet50_xgb.yml'
    IMAGE_CLASSIFICATION = 'image_classification_resnet50_xgb.yml'
    IMAGE_OBJECT_DETECTION = 'image_object_detection_yolo.yml'

    # TEXT
    TEXT_CLASSIFICATION = 'text_classification_encoding_xgb.yml'
    TEXT_REGRESSION = 'text_regression_encoding_xgb.yml'

    # GRAPH
    GRAPH_COMMUNITY_DETECTION = 'graph_community_detection.yml'
    # GRAPH_COMMUNITY_DETECTION_DISTIL = 'graph_community_detection_distil.yml'
    GRAPH_LINK_PREDICTION = 'graph_link_prediction_distil.yml'
    GRAPH_MATCHING = 'graph_matching.yml'
    # GRAPH_MATCHING_JHU = 'graph_matching_jhu.yml'


def detect_data_modality(dataset_doc_path):
    with open(dataset_doc_path) as f:
        dataset_doc = json.load(f)

    resources = list()
    for resource in dataset_doc['dataResources']:
        resources.append(resource['resType'])

    if len(resources) == 1:
        return 'single_table'
    else:
        for resource in resources:
            if resource == 'edgeList':
                return 'graph'
            elif resource not in ('table', 'raw'):
                return resource

    return 'multi_table'


def get_dataset_details(dataset, problem):
    data_modality = detect_data_modality(dataset)
    task_type = problem['problem']['task_type'].name.lower()
    task_subtype = problem['problem']['task_subtype'].name.lower()

    return data_modality, task_type, task_subtype


def to_dicts(hyperparameters):

    params_tree = defaultdict(dict)
    for (block, hyperparameter), value in hyperparameters.items():
        if isinstance(value, np.integer):
            value = int(value)

        elif isinstance(value, np.floating):
            value = float(value)

        elif isinstance(value, np.ndarray):
            value = value.tolist()

        elif isinstance(value, np.bool_):
            value = bool(value)

        elif value == 'None':
            value = None

        params_tree[block][hyperparameter] = value

    return params_tree


FILE_COLLECTION = 'https://metadata.datadrivendiscovery.org/types/FilesCollection'
GRAPH = 'https://metadata.datadrivendiscovery.org/types/Graph'
EDGE_LIST = 'https://metadata.datadrivendiscovery.org/types/EdgeList'


class PipelineSearcher:

    def _find_dataset(self, dataset_id):
        train = 'TRAIN' in dataset_id
        for dataset_name in os.scandir(self.input):
            dataset_root = os.path.join(self.input, dataset_name)
            if train:
                dataset_doc_dir = os.path.join(dataset_root, 'TRAIN', 'dataset_TRAIN')
            else:
                dataset_doc_dir = os.path.join(dataset_root, dataset_name + '_dataset')

            dataset_doc_path = os.path.join(dataset_doc_dir, 'datasetDoc.json')

            LOGGER.info('Loading datasetDoc from %s', dataset_doc_path)
            with open(dataset_doc_path, 'r') as dataset_doc_file:
                dataset_doc = json.load(dataset_doc_file)

            if dataset_doc['about']['datasetID'] == dataset_id:
                LOGGER.info('Dataset_id %s found!', dataset_id)
                dataset_path = 'file://' + os.path.abspath(dataset_doc_path)

                return dataset_name, dataset_path

        raise ValueError('Cannot find dataset {}'.format(dataset_id))

    def _get_dataset_details(self, problem):
        dataset_id = problem['inputs'][0]['dataset_id']
        problem_paths = problem.get('location_uris')
        dataset_root = None
        if problem_paths:
            problem_path = problem_paths[0].replace('file://', '')

            # /path/to/dataset_name/TRAIN/problem_TRAIN/problemDoc.json
            dataset_root = os.path.realpath(os.path.join(
                os.path.dirname(problem_path),
                os.path.pardir,
            ))
            dataset_name = os.path.basename(
                os.path.realpath(os.path.join(dataset_root, os.pardir)))

        elif 'TRAIN' in dataset_id:
            dataset_name = dataset_id.replace('_dataset_TRAIN', '')
            dataset_root = os.path.join(self.input, dataset_name, 'TRAIN')

        if dataset_root:
            # /path/to/dataset_name/TRAIN
            dataset_path = os.path.join(dataset_root, 'dataset_TRAIN', 'datasetDoc.json')

            if os.path.exists(dataset_path):
                with open(dataset_path, 'r') as dataset_doc_file:
                    dataset_doc = json.load(dataset_doc_file)

                if dataset_doc['about']['datasetID'] == dataset_id:
                    dataset_name = os.path.basename(
                        os.path.realpath(os.path.join(dataset_root, os.pardir)))
                    dataset_path = 'file://' + dataset_path

                    return dataset_name, dataset_path

        LOGGER.warn('Dataset ID not found. Searching inside the input dir')
        return self._find_dataset(dataset_id)

    def _load_pipeline(self, pipeline):
        if pipeline.endswith('.yml'):
            loader = Pipeline.from_yaml
        else:
            loader = Pipeline.from_json
            if not pipeline.endswith('.json'):
                pipeline += '.json'

        path = os.path.join(PIPELINES_DIR, pipeline)
        with open(path, 'r') as pipeline_file:
            return loader(string_or_file=pipeline_file)

    def _get_templates(self, data_modality, task_type):
        LOGGER.info("Loading template for data modality %s and task type %s",
                    data_modality, task_type)

        templates = [Templates.SINGLE_TABLE_CLASSIFICATION_ENC_XGB]

        if data_modality == 'single_table':
            if task_type == TaskType.CLASSIFICATION.name.lower():
                templates = [
                    Templates.SINGLE_TABLE_CLASSIFICATION_ENC_XGB,
                    Templates.SINGLE_TABLE_CLASSIFICATION_AR_RF,
                    Templates.SINGLE_TABLE_CLASSIFICATION_DFS_ROBUST_XGB,
                ]
            elif task_type == TaskType.REGRESSION.name.lower():
                templates = [
                    Templates.SINGLE_TABLE_REGRESSION_XGB,
                    Templates.SINGLE_TABLE_REGRESSION_SC_XGB,
                    Templates.SINGLE_TABLE_REGRESSION_ENC_XGB,
                ]
            elif task_type == TaskType.COLLABORATIVE_FILTERING.name.lower():
                templates = [
                    Templates.SINGLE_TABLE_REGRESSION_XGB,
                    Templates.SINGLE_TABLE_REGRESSION_SC_XGB,
                    Templates.SINGLE_TABLE_REGRESSION_ENC_XGB,
                ]
            elif task_type == TaskType.TIME_SERIES_FORECASTING.name.lower():
                templates = [
                    Templates.SINGLE_TABLE_REGRESSION_XGB,
                    Templates.SINGLE_TABLE_REGRESSION_SC_XGB,
                    Templates.SINGLE_TABLE_REGRESSION_ENC_XGB,
                ]
            elif task_type == TaskType.SEMISUPERVISED_CLASSIFICATION.name.lower():
                templates = [Templates.SINGLE_TABLE_SEMI_CLASSIFICATION]
            elif task_type == TaskType.CLUSTERING.name.lower():
                templates = [Templates.SINGLE_TABLE_CLUSTERING]

        if data_modality == 'multi_table':
            if task_type == TaskType.CLASSIFICATION.name.lower():
                templates = [
                    Templates.MULTI_TABLE_CLASSIFICATION_LDA_LOGREG,
                    Templates.MULTI_TABLE_CLASSIFICATION_DFS_XGB,
                ]
            elif task_type == TaskType.REGRESSION.name.lower():
                templates = [Templates.MULTI_TABLE_REGRESSION]
        elif data_modality == 'text':
            if task_type == TaskType.CLASSIFICATION.name.lower():
                templates = [Templates.TEXT_CLASSIFICATION]
            elif task_type == TaskType.REGRESSION.name.lower():
                templates = [Templates.TEXT_REGRESSION]

        if data_modality == 'timeseries':
            templates = [
                Templates.TIMESERIES_CLASSIFICATION_KN,
                Templates.TIMESERIES_CLASSIFICATION_DSBOX_LR,
                Templates.TIMESERIES_CLASSIFICATION_LSTM_FCN
            ]
            # if task_type == TaskType.CLASSIFICATION.name.lower():
            #     template = Templates.TIMESERIES_CLASSIFICATION
            # elif task_type == TaskType.REGRESSION.name.lower():
            #     template = Templates.TIMESERIES_REGRESSION
        elif data_modality == 'image':
            if task_type == TaskType.CLASSIFICATION.name.lower():
                templates = [Templates.IMAGE_CLASSIFICATION]
            elif task_type == TaskType.REGRESSION.name.lower():
                templates = [Templates.IMAGE_REGRESSION]
            elif task_type == TaskType.OBJECT_DETECTION.name.lower():
                templates = [Templates.IMAGE_OBJECT_DETECTION]

        if data_modality == 'graph':
            if task_type == TaskType.COMMUNITY_DETECTION.name.lower():
                templates = [Templates.GRAPH_COMMUNITY_DETECTION]
            elif task_type == TaskType.LINK_PREDICTION.name.lower():
                templates = [Templates.GRAPH_LINK_PREDICTION]
            elif task_type == TaskType.GRAPH_MATCHING.name.lower():
                templates = [Templates.GRAPH_MATCHING]
            elif task_type == TaskType.VERTEX_CLASSIFICATION.name.lower():
                templates = [Templates.SINGLE_TABLE_CLASSIFICATION_ENC_XGB]

        return [template.value for template in templates]

    def __init__(self, input_dir='input', output_dir='output', static_dir='static',
                 dump=False, hard_timeout=False):
        self.input = input_dir
        self.output = output_dir
        self.static = static_dir
        self.dump = dump
        self.hard_timeout = hard_timeout
        self.subprocess = None

        self.ranked_dir = os.path.join(self.output, 'pipelines_ranked')
        self.scored_dir = os.path.join(self.output, 'pipelines_scored')
        self.searched_dir = os.path.join(self.output, 'pipelines_searched')
        os.makedirs(self.ranked_dir, exist_ok=True)
        os.makedirs(self.scored_dir, exist_ok=True)
        os.makedirs(self.searched_dir, exist_ok=True)

        self.solutions = list()
        self.data_pipeline = self._load_pipeline('kfold_pipeline.yml')
        self.scoring_pipeline = self._load_pipeline(DEFAULT_SCORING_PIPELINE_PATH)
        self.fallback = self._load_pipeline(FALLBACK_PIPELINE)

    @staticmethod
    def _evaluate(out, pipeline, *args, **kwargs):
        LOGGER.info('Running d3m.runtime.evalute on pipeline %s', pipeline.id)
        results = d3m_evaluate(pipeline, *args, **kwargs)

        LOGGER.info('Returning results for %s', pipeline.id)
        out.extend(results)

    def subprocess_evaluate(self, pipeline, *args, **kwargs):
        LOGGER.info('Evaluating pipeline %s in a subprocess', pipeline.id)
        with Manager() as manager:
            output = manager.list()
            process = Process(
                target=self._evaluate,
                args=(output, pipeline, *args),
                kwargs=kwargs
            )
            self.subprocess = process
            process.daemon = True
            process.start()

            LOGGER.info('Joining process %s', process.pid)
            process.join()

            LOGGER.info('Terminating process %s', process.pid)
            process.terminate()
            self.subprocess = None

            result = tuple(output) if output else None

        if not result:
            raise Exception("Evaluate crashed")

        return result

    def score_pipeline(self, dataset, problem, pipeline, metrics=None, random_seed=0,
                       folds=5, stratified=False, shuffle=False):

        problem_metrics = problem['problem']['performance_metrics']
        metrics = metrics or problem_metrics
        data_params = {
            'number_of_folds': json.dumps(folds),
            'stratified': json.dumps(stratified),
            'shuffle': json.dumps(shuffle),
        }

        # Some primitives crash with a core dump that kills everything.
        # We want to isolate those.
        primitives = [
            step['primitive']['python_path']
            for step in pipeline.to_json_structure()['steps']
        ]
        if any(primitive in SUBPROCESS_PRIMITIVES for primitive in primitives):
            evaluate = self.subprocess_evaluate
        else:
            evaluate = d3m_evaluate

        all_scores, all_results = evaluate(
            pipeline,
            self.data_pipeline,
            self.scoring_pipeline,
            problem,
            [dataset],
            data_params,
            metrics,
            context=Context.TESTING,
            random_seed=random_seed,
            data_random_seed=random_seed,
            scoring_random_seed=random_seed,
            volumes_dir=self.static,
        )

        if not all_scores:
            failed_result = all_results[-1]
            message = failed_result.pipeline_run.status['message']
            LOGGER.error(message)
            cause = failed_result.error.__cause__
            if isinstance(cause, BaseException):
                raise cause
            else:
                raise Exception(cause)

        pipeline.cv_scores = [score.value[0] for score in all_scores]
        pipeline.score = np.mean(pipeline.cv_scores)

    def _save_pipeline(self, pipeline):
        pipeline_dict = pipeline.to_json_structure()

        if pipeline.score is None:
            dump_pipeline(pipeline_dict, self.searched_dir)
        else:
            dump_pipeline(pipeline_dict, self.scored_dir)

            rank = (1 - pipeline.normalized_score) + random.random() * 1.e-12   # avoid collisions
            if self.dump:
                dump_pipeline(pipeline_dict, self.ranked_dir, rank)

            pipeline_dict['rank'] = rank
            pipeline_dict['score'] = pipeline.score
            pipeline_dict['normalized_score'] = pipeline.normalized_score
            self.solutions.append(pipeline_dict)

    @staticmethod
    def _new_pipeline(pipeline, hyperparams=None):
        hyperparams = to_dicts(hyperparams) if hyperparams else dict()

        new_pipeline = Pipeline()
        for input_ in pipeline.inputs:
            new_pipeline.add_input(name=input_['name'])

        for step_id, old_step in enumerate(pipeline.steps):
            new_step = PrimitiveStep(primitive=old_step.primitive)
            for name, argument in old_step.arguments.items():
                new_step.add_argument(
                    name=name,
                    argument_type=argument['type'],
                    data_reference=argument['data']
                )
            for output in old_step.outputs:
                new_step.add_output(output)

            new_hyperparams = hyperparams.get(str(step_id), dict())
            for name, hyperparam in old_step.hyperparams.items():
                if name not in new_hyperparams:
                    new_step.add_hyperparameter(
                        name=name,
                        argument_type=ArgumentType.VALUE,
                        data=hyperparam['data']
                    )

            for name, value in new_hyperparams.items():
                new_step.add_hyperparameter(
                    name=name,
                    argument_type=ArgumentType.VALUE,
                    data=value
                )

            new_pipeline.add_step(new_step)

        for output in pipeline.outputs:
            new_pipeline.add_output(
                name=output['name'],
                data_reference=output['data']
            )

        new_pipeline.cv_scores = list()
        new_pipeline.score = None

        return new_pipeline

    def check_stop(self):
        now = datetime.now()

        if (self._stop or (self.timeout and (now > self.max_end_time))):
            raise KeyboardInterrupt()

    def stop(self):
        self._stop = True
        # if self.subprocess:
        #     LOGGER.info('Terminating subprocess: %s', self.subprocess.pid)
        #     self.subprocess.terminate()
        #     self.subprocess = None

    def _timeout(self, *args, **kwargs):
        raise KeyboardInterrupt()

    def setup_search(self):
        self.solutions = list()
        self._stop = False
        self.done = False

        self.start_time = datetime.now()
        self.max_end_time = None
        if self.timeout:
            self.max_end_time = self.start_time + timedelta(seconds=self.timeout)

            if self.hard_timeout:
                signal.signal(signal.SIGALRM, self._timeout)
                signal.alarm(self.timeout)

        LOGGER.info("Timeout: %s (Hard: %s); Max end: %s",
                    self.timeout, self.hard_timeout, self.max_end_time)

    def get_data_augmentation(self, dataset, problem):
        datamart = RESTDatamart(DATAMART_URL)
        data_augmentation = problem.get('data_augmentation')
        if data_augmentation:
            LOGGER.info("DATA AUGMENTATION: Querying DataMart")
            try:
                keywords = data_augmentation[0]['keywords']
                query = DatamartQuery(keywords=keywords)
                cursor = datamart.search_with_data(query=query, supplied_data=dataset)
                LOGGER.info("DATA AUGMENTATION: Getting next page")
                page = cursor.get_next_page()
                if page:
                    result = page[0]
                    return result.serialize()

            except Exception:
                LOGGER.exception("DATA AUGMENTATION ERROR")

        # TODO: Replace this with the real DataMart query
        # if problem['id'] == 'DA_ny_taxi_demand_problem_TRAIN':
        #     LOGGER.info('DATA AUGMENTATION!!!!!!')
        #     with open(os.path.join(BASE_DIR, 'da.json')) as f:
        #         return json.dumps(json.load(f))

    def search(self, problem, timeout=None, budget=None, template_names=None):

        self.timeout = timeout
        best_pipeline = None
        best_score = None
        best_normalized = 0
        best_template_name = None
        template_names = template_names or list()
        data_modality = None
        task_type = None
        task_subtype = None
        iteration = 0
        errors = list()

        dataset_name, dataset_path = self._get_dataset_details(problem)
        dataset = Dataset.load(dataset_path)
        metric = problem['problem']['performance_metrics'][0]['metric']

        data_modality = detect_data_modality(dataset_path[7:])
        task_type = problem['problem']['task_type'].name.lower()
        task_subtype = problem['problem']['task_subtype'].name.lower()

        data_augmentation = self.get_data_augmentation(dataset, problem)

        LOGGER.info("Searching dataset %s: %s/%s/%s",
                    dataset_name, data_modality, task_type, task_subtype)

        try:
            self.setup_search()

            self.score_pipeline(dataset, problem, self.fallback)
            self.fallback.normalized_score = metric.normalize(self.fallback.score)
            self._save_pipeline(self.fallback)
            best_pipeline = self.fallback.id
            best_score = self.fallback.score
            best_template_name = FALLBACK_PIPELINE
            best_normalized = self.fallback.normalized_score

            LOGGER.info("Fallback pipeline score: %s - %s",
                        self.fallback.score, self.fallback.normalized_score)

            LOGGER.info("Loading the template and the tuner")
            if not template_names:
                template_names = self._get_templates(data_modality, task_type)

            if budget is not None:
                iterator = range(budget)
            else:
                iterator = itertools.count()   # infinite range

            selector_tuner = SelectorTuner(template_names, data_augmentation)

            for iteration in iterator:
                self.check_stop()
                template_name, template, proposal, defaults = selector_tuner.propose()
                pipeline = self._new_pipeline(template, proposal)

                params = '\n'.join('{}: {}'.format(k, v) for k, v in proposal.items())
                LOGGER.warn("Scoring pipeline %s - %s: %s\n%s",
                            iteration + 1, template_name, pipeline.id, params)
                try:
                    self.score_pipeline(dataset, problem, pipeline)
                    pipeline.normalized_score = metric.normalize(pipeline.score)
                    # raise Exception("This won't work")
                except Exception as ex:
                    LOGGER.exception("Error scoring pipeline %s for dataset %s",
                                     pipeline.id, dataset_name)

                    if defaults:
                        error = '{}: {}'.format(type(ex).__name__, ex)
                        errors.append(error)
                        max_errors = min(len(selector_tuner.template_names), budget or np.inf)
                        if len(errors) >= max_errors:
                            raise Exception(errors)

                    pipeline.score = None
                    pipeline.normalized_score = 0.0

                try:
                    self._save_pipeline(pipeline)
                except Exception:
                    LOGGER.exception("Error saving pipeline %s", pipeline.id)

                selector_tuner.add(template_name, proposal, pipeline.normalized_score)
                LOGGER.info("Pipeline %s score: %s - %s",
                            pipeline.id, pipeline.score, pipeline.normalized_score)

                if pipeline.normalized_score > best_normalized:
                    LOGGER.warn("New best pipeline found: %s! %s is better than %s",
                                template_name, pipeline.score, best_score)
                    best_pipeline = pipeline.id
                    best_score = pipeline.score
                    best_normalized = pipeline.normalized_score
                    best_template_name = template_name

        except KeyboardInterrupt:
            pass
        except Exception:
            LOGGER.exception("Error processing dataset %s", dataset)

        finally:
            if self.timeout and self.hard_timeout:
                signal.alarm(0)

        self.done = True
        iterations = iteration - len(template_names) + 1
        if iterations <= 0:
            iterations = None

        return {
            'pipeline': best_pipeline,
            'cv_score': best_score,
            'template': best_template_name,
            'data_modality': data_modality,
            'task_type': task_type,
            'task_subtype': task_subtype,
            'tuning_iterations': iterations,
            'error': errors or None
        }
