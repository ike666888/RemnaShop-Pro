import ast
import json
import unittest
from pathlib import Path


class TestPanelApiIpControlContract(unittest.TestCase):
    def _load_ip_control_endpoint_specs(self):
        source = Path('services/panel_api.py').read_text(encoding='utf-8')
        module = ast.parse(source)
        for node in module.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == 'IP_CONTROL_ENDPOINT_SPECS':
                        return ast.literal_eval(node.value)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == 'IP_CONTROL_ENDPOINT_SPECS':
                return ast.literal_eval(node.value)
        self.fail('IP_CONTROL_ENDPOINT_SPECS not found in services/panel_api.py')

    def test_ip_control_paths_are_declared_in_openapi(self):
        endpoint_specs = self._load_ip_control_endpoint_specs()
        spec = json.loads(Path('docs/remnawave-openapi.json').read_text(encoding='utf-8'))
        paths = spec.get('paths', {})

        missing = []
        for method, endpoint in endpoint_specs:
            openapi_path = f"/api{endpoint}"
            methods = paths.get(openapi_path, {})
            if method.lower() not in methods:
                missing.append(f"{method} {openapi_path}")

        self.assertEqual(missing, [], msg=f"IP control endpoints missing from OpenAPI: {missing}")


if __name__ == '__main__':
    unittest.main()
