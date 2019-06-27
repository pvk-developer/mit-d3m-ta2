import argparse
import logging
import os
import traceback
from datetime import datetime

import pandas as pd
import tabulate
from d3m.container.dataset import Dataset
from d3m.metadata.base import Context
from d3m.metadata.pipeline import Pipeline
from d3m.metadata.problem import Problem
from d3m.runtime import Runtime, score

from ta2 import logging_setup
from ta2.search import PipelineSearcher
from ta2.ta3.client import TA3APIClient
from ta2.ta3.server import serve
from ta2.utils import ensure_downloaded


def load_dataset(root_path, phase, inner_phase=None):
    inner_phase = inner_phase or phase
    path = os.path.join(root_path, phase, 'dataset_' + inner_phase, 'datasetDoc.json')
    return Dataset.load(dataset_uri='file://' + os.path.abspath(path))


def load_problem(root_path, phase):
    path = os.path.join(root_path, phase, 'problem_' + phase, 'problemDoc.json')
    return Problem.load(problem_uri='file://' + os.path.abspath(path))


def load_pipeline(pipeline_path):
    with open(pipeline_path, 'r') as pipeline_file:
        if pipeline_path.endswith('.json'):
            return Pipeline.from_json(pipeline_file)
        else:
            return Pipeline.from_yaml(pipeline_file)


def search(dataset_root, problem, args):

    pps = PipelineSearcher(args.input, args.output, dump=True)

    return pps.search(problem, timeout=args.timeout, budget=args.budget)


def score_pipeline(dataset_root, problem, pipeline_path):
    train_dataset = load_dataset(dataset_root, 'TRAIN')
    test_dataset = load_dataset(dataset_root, 'SCORE', 'TEST')
    pipeline = load_pipeline(pipeline_path)

    # Creating an instance on runtime with pipeline description and problem description.
    runtime = Runtime(
        pipeline=pipeline,
        problem_description=problem,
        context=Context.TESTING
    )

    print("Fitting the pipeline")
    fit_results = runtime.fit(inputs=[train_dataset])
    fit_results.check_success()

    # Producing results using the fitted pipeline.
    print("Producing predictions")
    produce_results = runtime.produce(inputs=[test_dataset])
    produce_results.check_success()

    predictions = produce_results.values['outputs.0']
    metrics = problem['problem']['performance_metrics']

    print("Computing the score")
    scoring_pipeline = load_pipeline('ta2/pipelines/scoring_pipeline.yml')
    scores, scoring_pipeline_run = score(
        scoring_pipeline, problem, predictions, [test_dataset], metrics,
        context=Context.TESTING, random_seed=0,
    )
    return scores.iloc[0].value


def box_print(message, strong=False):
    char = '#' if strong else '*'
    print(char * len(message))
    print(message)
    print(char * len(message))


def process_dataset(dataset, args):
    start_ts = datetime.utcnow()

    box_print("Processing dataset {}".format(dataset), True)
    ensure_downloaded(dataset, args.input)
    dataset_root = os.path.join(args.input, dataset)
    problem = load_problem(dataset_root, 'TRAIN')

    print("Searching Pipeline for dataset {}".format(dataset))
    result = search(dataset_root, problem, args)
    result['elapsed_time'] = datetime.utcnow() - start_ts
    result['dataset'] = dataset

    pipeline_id = result['pipeline']
    cv_score = result['cv_score']
    if cv_score is not None:
        box_print("Best Pipeline: {} - CV Score: {}".format(pipeline_id, cv_score))

        pipeline_path = os.path.join(args.output, 'pipelines_ranked', pipeline_id + '.json')
        test_score = score_pipeline(dataset_root, problem, pipeline_path)
        box_print("Test Score for pipeline {}: {}".format(pipeline_id, test_score))

        result['test_score'] = test_score

    return result


REPORT_COLUMNS = [
    'dataset',
    'template',
    'cv_score',
    'test_score',
    'elapsed_time',
    'tuning_iterations',
    'data_modality',
    'task_type'
]


def _ta2_test(args):
    results = list()
    for dataset in args.dataset:
        try:
            results.append(process_dataset(dataset, args))
        except Exception as ex:
            box_print("Error processing dataset {}".format(dataset), True)
            traceback.print_exc()
            results.append({
                'dataset': dataset,
                'error': str(ex)
            })

    report = pd.DataFrame(
        results,
        columns=REPORT_COLUMNS
    )

    if args.report:
        # dump to file
        report.to_csv(args.report, index=False)
    else:
        # print to stdout
        print(tabulate.tabulate(
            report,
            showindex=False,
            tablefmt='github',
            headers=REPORT_COLUMNS
        ))


def _ta3_test_dataset(client, dataset, timeout):
    print('### Testing dataset {} ###'.format(dataset))

    print('### {} => client.SearchSolutions("{}")'.format(dataset, dataset))
    search_response = client.search_solutions(dataset, timeout)
    search_id = search_response.search_id

    print('### {} => client.GetSearchSolutionsResults("{}")'.format(dataset, search_id))
    solutions = client.get_search_solutions_results(search_id, 2)
    solution_id = solutions[-1].solution_id

    print('### {} => client.StopSearchSolutions("{}")'.format(dataset, solution_id))
    client.stop_search_solutions(search_id)

    print('### {} => client.DescribeSolution("{}")'.format(dataset, search_id))
    client.describe_solution(solution_id)

    print('### {} => client.ScoreSolution("{}")'.format(dataset, solution_id))
    score_response = client.score_solution(solution_id, dataset)
    request_id = score_response.request_id

    print('### {} => client.GetScoreSolutionsResults("{}")'.format(dataset, request_id))
    client.get_score_solution_results(request_id)

    print('### {} => client.FitSolution("{}")'.format(dataset, solution_id))
    fit_response = client.fit_solution(solution_id, dataset)
    request_id = fit_response.request_id

    print('### {} => client.GetFitSolutionsResults("{}")'.format(dataset, request_id))
    fitted_solutions = client.get_fit_solution_results(request_id)
    fitted_solution_id = fitted_solutions[-1].fitted_solution_id

    print('### {} => client.ProduceSolution("{}")'.format(dataset, fitted_solution_id))
    produce_response = client.produce_solution(fitted_solution_id, dataset)
    request_id = produce_response.request_id

    print('### {} => client.GetProduceSolutionsResults("{}")'.format(dataset, request_id))
    client.get_produce_solution_results(request_id)

    print('### {} => client.SolutionExport("{}")'.format(dataset, fitted_solution_id))
    client.solution_export(fitted_solution_id, 1)

    print('### {} => client.EndSearchSolutions("{}")'.format(dataset, search_id))
    client.end_search_solutions(search_id)


def _ta3_test(args):
    local_input = args.input
    remote_input = '/input' if args.docker else args.input
    client = TA3APIClient(args.port, local_input, remote_input)

    print('### Hello ###')
    client.hello()

    for dataset in args.dataset:
        ensure_downloaded(dataset, args.input)
        _ta3_test_dataset(client, dataset, args.timeout / 60)


def _server(args):
    input_dir = args.input or os.getenv('D3MINPUTDIR', 'input')
    output_dir = args.output or os.getenv('D3MOUTPUTDIR', 'output')
    timeout = args.timeout or os.getenv('D3MTIMEOUT', 600)

    try:
        timeout = int(timeout)
    except ValueError:
        # FIXME This is just to be sure that it does not crash
        timeout = 600

    serve(args.port, input_dir, output_dir, timeout, args.debug)


def parse_args(mode=None):

    # Logging
    logging_args = argparse.ArgumentParser(add_help=False)
    logging_args.add_argument('-v', '--verbose', action='count', default=0,
                              help='Be verbose. Use -vv for increased verbosity')
    logging_args.add_argument('-l', '--logfile', type=str, nargs='?',
                              help='Path to the logging file. If not given, log to stdout')

    # IO Specification
    io_args = argparse.ArgumentParser(add_help=False)
    io_args.add_argument('-i', '--input', default='input',
                         help='Path to the datsets root folder')
    io_args.add_argument('-o', '--output', default='output',
                         help='Path to the folder where outputs will be stored')

    # Datasets
    dataset_args = argparse.ArgumentParser(add_help=False)
    dataset_args.add_argument('dataset', nargs='+', help='Name of the dataset to use for the test')

    # Search Configuration
    search_args = argparse.ArgumentParser(add_help=False)
    search_args.add_argument('-t', '--timeout', type=int,
                             help='Maximum time allowed for the tuning, in number of seconds')

    # TA3-TA2 Common Args
    ta3_args = argparse.ArgumentParser(add_help=False)
    ta3_args.add_argument('--port', type=int, default=45042,
                          help='Port to use, both for client and server.')

    if mode == 'ta2':
        parser = argparse.ArgumentParser(
            description='TA2 in Standalone Mode',
            parents=[logging_args, io_args, search_args, dataset_args],
        )
        parser.add_argument(
            '-r', '--report',
            help='Path to the CSV file where scores will be dumped.')
        parser.add_argument(
            '-b', '--budget', type=int,
            help='Maximum number of tuning iterations to perform')

    elif mode == 'ta3':
        parser = argparse.ArgumentParser(
            description='TA3-TA2 API Test',
            parents=[logging_args, io_args, search_args, ta3_args, dataset_args],
        )
        parser.add_argument('--server', action='store_true', help=(
            'Start a server instance in background.'
        ))
        parser.add_argument('--docker', action='store_true', help=(
            'Adapt input paths to work with a dockerized TA2.'
        ))
    elif mode == 'server':
        parser = argparse.ArgumentParser(
            description='TA3-TA2 Server',
            parents=[logging_args, io_args, ta3_args, search_args],
        )
        parser.set_defaults(command=_server)
        parser.add_argument(
            '--debug', action='store_true',
            help='Start the server in sync mode. Needed for debugging.')

    args = parser.parse_args()
    logging_setup(args.verbose, args.logfile)
    logging.getLogger("d3m.metadata.pipeline_run").setLevel(logging.ERROR)

    return args


def ta2_test():
    args = parse_args('ta2')
    _ta2_test(args)


def ta3_test():
    args = parse_args('ta3')
    _ta3_test(args)


def ta2_server():
    args = parse_args('server')
    _server(args)
