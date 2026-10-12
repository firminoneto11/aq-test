cov := "coverage run -m pytest"
cov_port := "5500"
url := "http://localhost:{{cov_port}}"

init:
	uv init --build-backend hatch -p 3.14

env:
	rm -rf .venv/
	uv venv -p 3.14

deps:
	uv sync --frozen

lock:
	uv sync

upgrade:
	uv sync --upgrade

dev *args:
	python3 manage.py dev {{args}}

prod *args:
	python3 manage.py prod {{args}}

pc:
	pre-commit run --config .pre-commit-config.yaml --all-files

# Remove Python bytecode caches throughout the repo, including .venv.
clean:
	find . -type d -name '__pycache__' -prune -exec rm -rf -- {} +

cov:
	{{cov}}
	coverage report

hcov:
	{{cov}}
	coverage html
	python3 -c "import webbrowser; webbrowser.open_new_tab('{{url}}')"
	python3 -m http.server -d .coverage/html-report {{cov_port}}
