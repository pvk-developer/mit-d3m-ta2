.DEFAULT_GOAL := help

define BROWSER_PYSCRIPT
import os, webbrowser, sys

try:
	from urllib import pathname2url
except:
	from urllib.request import pathname2url

webbrowser.open("file://" + pathname2url(os.path.abspath(sys.argv[1])))
endef
export BROWSER_PYSCRIPT

define PRINT_HELP_PYSCRIPT
import re, sys

for line in sys.stdin:
	match = re.match(r'^([a-zA-Z_-]+):.*?## (.*)$$', line)
	if match:
		target, help = match.groups()
		print("%-20s %s" % (target, help))
endef
export PRINT_HELP_PYSCRIPT

BROWSER := python -c "$$BROWSER_PYSCRIPT"

.PHONY: help
help:
	@python -c "$$PRINT_HELP_PYSCRIPT" < $(MAKEFILE_LIST)

# CLEAN TARGETS

.PHONY: clean-build
clean-build: ## remove build artifacts
	rm -fr build/
	rm -fr dist/
	rm -fr .eggs/
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -fr {} +

.PHONY: clean-pyc
clean-pyc: ## remove Python file artifacts
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +

.PHONY: clean-docs
clean-docs: ## remove previously built docs
	rm -rf docs/build
	rm -rf docs/api
	$(MAKE) -C docs clean

.PHONY: clean-coverage
clean-coverage: ## remove coverage artifacts
	rm -f .coverage
	rm -f .coverage.*
	rm -fr htmlcov/

.PHONY: clean-test
clean-test: ## remove test artifacts
	rm -fr .tox/
	rm -fr .pytest_cache

.PHONY: clean
clean: clean-build clean-pyc clean-test clean-coverage clean-docs ## remove all build, test, coverage, docs and Python artifacts


# INSTALL TARGETS

.PHONY: install
install: clean-build clean-pyc ## install the package to the active Python's site-packages
	pip install . && pip install -r requirements.txt

.PHONY: install-test
install-test: clean-build clean-pyc ## install the package and test dependencies
	pip install .[test] && pip install -r requirements.txt

.PHONY: install-develop
install-develop: clean-build clean-pyc ## install the package in editable mode and dependencies for development
	pip install -e .[dev] && pip install -r requirements.txt

.PHONY: install-primitives
install-primitives: clean-build clean-pyc ## install the primitive packages to the active Python's site-packages
	pip install -r primitives_requirements.txt

.PHONY: install-system
install-system: ## install system requirements
	sudo apt-get install -y $(shell cat system_requirements.txt)

.PHONY: install-all
install-all: clean-build clean-pyc install-system install-develop install-primitives ## install-develop + additional requirements
	pip install -r devel_requirements.txt
	python -m d3m.index download -o static


# LINT TARGETS

.PHONY: lint
lint: ## check style with flake8 and isort
	flake8 ta2 tests
	isort -c --recursive ta2 tests

.PHONY: fix-lint
fix-lint: ## fix lint issues using autoflake, autopep8, and isort
	find ta2 -name '*.py' | xargs autoflake --in-place --remove-all-unused-imports --remove-unused-variables
	autopep8 --in-place --recursive --aggressive ta2
	isort --apply --atomic --recursive ta2

	find tests -name '*.py' | xargs autoflake --in-place --remove-all-unused-imports --remove-unused-variables
	autopep8 --in-place --recursive --aggressive tests
	isort --apply --atomic --recursive tests


# TEST TARGETS

.PHONY: test
test: ## run tests quickly with the default Python
	python -m pytest tests --cov=ta2

.PHONY: test-all
test-all: ## run tests on every Python version with tox
	tox -r

.PHONY: coverage
coverage: ## check code coverage quickly with the default Python
	coverage run --source ta2 -m pytest
	coverage report -m
	coverage html
	$(BROWSER) htmlcov/index.html


# DOCS TARGETS

.PHONY: docs
docs: clean-docs ## generate Sphinx HTML documentation, including API docs
	sphinx-apidoc --separate -T -o docs/api/ ta2
	$(MAKE) -C docs html

.PHONY: view-docs
view-docs: docs ## view docs in browser
	$(BROWSER) docs/_build/html/index.html

.PHONY: serve-docs
serve-docs: view-docs ## compile the docs watching for changes
	watchmedo shell-command -W -R -D -p '*.rst;*.md' -c '$(MAKE) -C docs html' .


# RELEASE TARGETS

.PHONY: dist
dist: clean ## builds source and wheel package
	python setup.py sdist
	python setup.py bdist_wheel
	ls -l dist

.PHONY: test-publish
test-publish: dist ## package and upload a release on TestPyPI
	twine upload --repository-url https://test.pypi.org/legacy/ dist/*

.PHONY: publish
publish: dist ## package and upload a release
	twine upload dist/*

.PHONY: bumpversion-release
bumpversion-release: ## Merge master to stable and bumpversion release
	git checkout stable
	git merge --no-ff master -m"make release-tag: Merge branch 'master' into stable"
	bumpversion release
	git push --tags origin stable

.PHONY: test-bumpversion-release
test-bumpversion-release: ## Merge master to stable and bumpversion release
	git checkout stable
	git merge --no-ff master -m"make release-tag: Merge branch 'master' into stable"
	bumpversion release

.PHONY: bumpversion-patch
bumpversion-patch: ## Merge stable to master and bumpversion patch
	git checkout master
	git merge stable
	bumpversion --no-tag patch
	git push

.PHONY: test-bumpversion-patch
test-bumpversion-patch: ## Merge stable to master and bumpversion patch
	git checkout master
	git merge stable
	bumpversion --no-tag patch

.PHONY: bumpversion-minor
bumpversion-minor: ## Bump the version the next minor skipping the release
	bumpversion --no-tag minor

.PHONY: bumpversion-major
bumpversion-major: ## Bump the version the next major skipping the release
	bumpversion --no-tag major

CURRENT_BRANCH := $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null)
CHANGELOG_LINES := $(shell git diff HEAD..stable HISTORY.md 2>/dev/null | wc -l)

.PHONY: check-release
check-release: ## Check if the release can be made
ifneq ($(CURRENT_BRANCH),master)
	$(error Please make the release from master branch\n)
endif
ifeq ($(CHANGELOG_LINES),0)
	$(error Please insert the release notes in HISTORY.md before releasing)
endif

.PHONY: release
release: check-release bumpversion-release bumpversion-patch

.PHONY: release-minor
release-minor: check-release bumpversion-minor release

.PHONY: release-major
release-major: check-release bumpversion-major release


# SUBMISSION TARGETS

.PHONY: login
login: ## Login to the d3m docker registry
	docker login registry.datadrivendiscovery.org

.PHONY: build
build: ## build the mit-d3m-ta2 docker image
	docker build -t mit-d3m-ta2 .

.PHONY: submit
submit: login build ## push to TA2 submission registry
	docker tag mit-d3m-ta2:latest registry.datadrivendiscovery.org/ta2-submissions/ta2-mit/summer2019
	docker push registry.datadrivendiscovery.org/ta2-submissions/ta2-mit/summer2019

.PHONY: submit-ci
submit-ci: login build ## push to TA2 submission registry
	docker tag mit-d3m-ta2:latest registry.datadrivendiscovery.org/jkanter/mit-fl-ta2:ci
	docker push registry.datadrivendiscovery.org/jkanter/mit-fl-ta2:ci

.PHONY: share
share: login build ## push to TA2 sharing registry
	docker tag mit-d3m-ta2:latest registry.datadrivendiscovery.org/jkanter/mit-fl-ta2:stable
	docker push registry.datadrivendiscovery.org/jkanter/mit-fl-ta2:stable

.PHONY: share
docker-test: login build ## push to TA2 sharing registry
	docker tag mit-d3m-ta2:latest registry.datadrivendiscovery.org/csala/mit-d3m-ta2:latest
	docker push registry.datadrivendiscovery.org/csala/mit-d3m-ta2:latest
