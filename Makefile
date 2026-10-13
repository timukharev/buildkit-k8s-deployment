.PHONY: test validate
test:
	python3 -m unittest discover -s tests -v
validate: test
	helm lint helm/sample-app
	@for environment in dev test prod; do \
		helm template sample-app helm/sample-app -f helm/sample-app/values-$$environment.yaml > /dev/null || exit 1; \
		kubectl kustomize k8s/build/overlays/$$environment > /dev/null || exit 1; \
	done
