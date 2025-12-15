// Jenkins Pipeline for E2E Tests (API Only)
// Runs API-based E2E tests against dev.dagknows.com or configured environment
// Repository: tests (not dagknows_src)

pipeline {
    agent {
        node {
            label 'docker'  // Use existing Docker agent - dependencies installed in venv
        }
    }

    environment {
        // Test environment configuration
        TEST_ENV = "${params.TEST_ENV ?: 'dev'}"
        DAGKNOWS_URL = "${params.DAGKNOWS_URL ?: 'https://dev.dagknows.com'}"
        DAGKNOWS_PROXY = "${params.DAGKNOWS_PROXY ?: '?proxy=dev1'}"
        
        // Test execution options
        TEST_MARKERS = "${params.TEST_MARKERS ?: 'api'}"  // Default to API tests only
        
        // Directories (tests repository structure)
        E2E_DIR = "${WORKSPACE}/e2e_tests"
        REPORTS_DIR = "${WORKSPACE}/e2e_tests/reports"
    }

    parameters {
        choice(
            name: 'TEST_ENV',
            choices: ['dev', 'staging', 'prod'],
            description: 'Target environment for E2E tests'
        )
        string(
            name: 'DAGKNOWS_URL',
            defaultValue: 'https://dev.dagknows.com',
            description: 'Base URL for DagKnows application'
        )
        string(
            name: 'DAGKNOWS_PROXY',
            defaultValue: '?proxy=dev1',
            description: 'Proxy parameter for requests'
        )
        string(
            name: 'TEST_MARKERS',
            defaultValue: 'api',
            description: 'Pytest markers to filter tests (e.g., "api and not slow")'
        )
        string(
            name: 'BRANCH',
            defaultValue: 'main',
            description: 'Git branch to checkout'
        )
    }

    stages {
        stage('Checkout') {
            steps {
                script {
                    withCredentials([usernamePassword(credentialsId: 'yash-dagknows-github-pat', usernameVariable: 'USERNAME', passwordVariable: 'GIT_TOKEN')]) {
                        sh """
                        git clone -b ${params.BRANCH} https://"\$GIT_TOKEN":x-oauth-basic@github.com/yash-dagknows/tests.git
                        cd tests
                        """
                    }
                }
            }
        }

        stage('Setup Test Environment') {
            steps {
                dir("${env.E2E_DIR}") {
                    script {
                        echo "Setting up Python virtual environment..."
                        echo "Creating virtual environment (with fallbacks)..."
                        sh """
                        #!/bin/bash
                        set -e
                        
                        # Try multiple methods to create virtual environment
                        # Method 1: Try python3 -m venv (requires python3-venv package)
                        if python3 -m venv venv 2>/dev/null; then
                            echo "✓ Virtual environment created with venv"
                        # Method 2: Try virtualenv command if available
                        elif command -v virtualenv > /dev/null 2>&1; then
                            virtualenv venv
                            echo "✓ Virtual environment created with virtualenv command"
                        # Method 3: Install virtualenv via pip and use it
                        else
                            echo "Installing virtualenv via pip..."
                            pip3 install --user virtualenv || pip3 install virtualenv
                            # Try using the installed virtualenv
                            if python3 -m virtualenv venv 2>/dev/null; then
                                echo "✓ Virtual environment created with pip-installed virtualenv"
                            elif ~/.local/bin/virtualenv venv 2>/dev/null; then
                                echo "✓ Virtual environment created with user-installed virtualenv"
                            else
                                echo "⚠️ Could not create virtual environment, trying without venv..."
                                # Last resort: install packages globally (not ideal but will work)
                                pip3 install --upgrade pip
                                pip3 install -r requirements.txt
                                echo "⚠️ Installed packages globally (no venv)"
                                exit 0
                            fi
                        fi
                        
                        # Activate venv and install dependencies (if venv was created)
                        if [ -d "venv" ]; then
                            source venv/bin/activate || . venv/bin/activate
                            pip install --upgrade pip
                            # Install all dependencies including Playwright (for future UI tests)
                            pip install -r requirements.txt
                            # Install Playwright browsers (even though we'll run API tests first)
                            # This ensures everything is ready when we enable UI tests later
                            playwright install chromium || echo "Playwright browser install skipped (will install per-user if needed)"
                        fi
                        """
                    }
                }
            }
        }

        stage('Configure Test Environment') {
            steps {
                dir("${env.E2E_DIR}") {
                    script {
                        // Get JWT token from Jenkins credentials
                        withCredentials([string(credentialsId: 'dagknows-jwt-token', variable: 'JWT_TOKEN')]) {
                            script {
                                // Log token info (first and last 20 chars for verification) without using unsupported methods
                                def rawToken = JWT_TOKEN
                                if (rawToken) {
                                    int previewLen = 20
                                    int tokLen = rawToken.length()
                                    String startPart = tokLen > previewLen ? rawToken.substring(0, previewLen) : rawToken
                                    String endPart = tokLen > previewLen ? rawToken.substring(tokLen - previewLen, tokLen) : rawToken
                                    def tokenPreview = "${startPart}...${endPart}"
                                    echo "Using JWT token from Jenkins credentials (preview: ${tokenPreview})"

                                    // Verify token starts with expected header
                                    def expectedTokenStart = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
                                    if (rawToken.startsWith(expectedTokenStart)) {
                                        echo "✓ JWT token format verified (matches expected token header)"
                                    } else {
                                        echo "⚠️ JWT token format may be different from expected header"
                                    }
                                } else {
                                    echo "⚠️ JWT token from credentials is empty or not set"
                                }
                            }

                            sh """
                            # Create .env file from template
                            cp env.template .env
                            
                            # Set environment variables
                            echo "DAGKNOWS_URL=${env.DAGKNOWS_URL}" >> .env
                            echo "DAGKNOWS_PROXY=${env.DAGKNOWS_PROXY}" >> .env
                            echo "DAGKNOWS_TOKEN=\${JWT_TOKEN}" >> .env
                            echo "TEST_USER_EMAIL=yash+user@dagknows.com" >> .env
                            echo "TEST_USER_PASSWORD=1Hey2Yash*" >> .env
                            echo "TEST_ORG=dagknows" >> .env
                            """
                        }
                    }
                }
            }
        }

        stage('Run API E2E Tests') {
            steps {
                dir("${env.E2E_DIR}") {
                    script {
                        def markerFilter = env.TEST_MARKERS ? "-m '${env.TEST_MARKERS}'" : "-m 'api'"
                        sh """
#!/bin/bash
set -e

# Activate venv if it exists, otherwise use system Python
if [ -d "venv" ]; then
    source venv/bin/activate || . venv/bin/activate
else
    echo "⚠️ Using system Python (no venv available)"
fi

# Set PYTHONPATH to include current directory so imports work
if [ -z "\$PYTHONPATH" ]; then
    export PYTHONPATH="${env.E2E_DIR}"
else
    export PYTHONPATH="${env.E2E_DIR}:\$PYTHONPATH"
fi
echo "PYTHONPATH: \$PYTHONPATH"
echo "Running pytest with markers: ${markerFilter}"

mkdir -p reports

# Run pytest and always generate JSON report + human summary, even on failures
set +e
pytest api_tests/ -v ${markerFilter} \\
    --json-report --json-report-file=reports/api-tests-report.json
pytest_exit=\$?
set -e

        python - << 'PY'
import json, os

report_path = os.path.join("reports", "api-tests-report.json")
summary_out = "test_summary.txt"
summary = "Test summary not available; see console output."

if os.path.exists(report_path):
    with open(report_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    s = data.get("summary", {})
    passed = s.get("passed", 0)
    failed = s.get("failed", 0)
    error = s.get("error", 0)
    skipped = s.get("skipped", 0)
    total = passed + failed + error + skipped
    summary = f"{passed} passed, {failed} failed, {error} errors, {skipped} skipped (total {total})"

with open(summary_out, "w", encoding="utf-8") as f:
    f.write(summary)

print("Pytest summary:", summary)
PY

exit \$pytest_exit
                        """
                    }
                }
            }
        }

        // UI E2E Tests Stage (commented out - will be enabled later)
        // Uncomment this stage when ready to run UI tests
        // 
        // When uncommenting, use this stage:
        // stage('Run UI E2E Tests') {
        //     steps {
        //         dir("${env.E2E_DIR}") {
        //             script {
        //                 sh "mkdir -p ${env.REPORTS_DIR}"
        //                 sh """
        //                 export DISPLAY=:99
        //                 Xvfb :99 -screen 0 1024x768x24 > /dev/null 2>&1 &
        //                 """
        //                 def markerFilter = env.TEST_MARKERS ? "-m '${env.TEST_MARKERS}'" : "-m 'ui'"
        //                 sh """
        //                 source venv/bin/activate
        //                 pytest ui_tests/ -v \\
        //                     --html=${env.REPORTS_DIR}/ui-report.html \\
        //                     --self-contained-html \\
        //                     --junitxml=${env.REPORTS_DIR}/ui-junit.xml \\
        //                     ${markerFilter} || true
        //                 """
        //             }
        //         }
        //     }
        //     post {
        //         always {
        //             archiveArtifacts artifacts: "${env.REPORTS_DIR}/ui-report.html, ${env.REPORTS_DIR}/ui-junit.xml"
        //             archiveArtifacts artifacts: "${env.REPORTS_DIR}/screenshots/"
        //         }
        //     }
        // }
    }

    post {
        always {
            echo "Test execution completed. Check console output above for results."
        }
        success {
            echo "✅ E2E API tests completed successfully - all tests passed!"
            // Optional Slack notification on success (via incoming webhook)
            // Requires a Jenkins secret text credential with ID 'slack-e2e-webhook-url'
            withCredentials([string(credentialsId: 'slack-e2e-webhook-url', variable: 'SLACK_WEBHOOK')]) {
                // Precompute build URL in Groovy so it's always available inside the shell
                def slackBuildUrl = env.BUILD_URL ?: (env.RUN_DISPLAY_URL ?: "")
                withEnv(["SLACK_BUILD_URL=${slackBuildUrl}"]) {
                    sh '''#!/bin/bash
set -e
if [ -z "$SLACK_WEBHOOK" ]; then
  echo "SLACK_WEBHOOK not set; skipping Slack notification"
  exit 0
fi

# Read test summary if available
summary_file="$WORKSPACE/e2e_tests/test_summary.txt"
if [ -f "$summary_file" ]; then
  summary=$(cat "$summary_file")
else
  summary="summary not available; see console"
fi

# Build a rich Slack Block Kit payload without requiring python
cat > slack_payload.json <<EOF
{
  "blocks": [
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "✅ *E2E API Tests PASSED - ${JOB_NAME} #${BUILD_NUMBER}*"
      }
    },
    {
      "type": "section",
      "fields": [
        {
          "type": "mrkdwn",
          "text": "*Result:*\\n✅ PASSED"
        },
        {
          "type": "mrkdwn",
          "text": "*Jenkins Run:*\\n<${SLACK_BUILD_URL}|Open build>"
        }
      ]
    },
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "*Summary:*\\n${summary}"
      }
    }
  ]
}
EOF

curl -sS -X POST -H 'Content-type: application/json' \
     --data @slack_payload.json \
     "$SLACK_WEBHOOK" || echo "Slack notification failed (non-fatal)"
                    '''
                }
            }
        }
        failure {
            echo "❌ E2E API tests failed - check console output above for details"
            // Optional Slack notification on failure (via incoming webhook)
            withCredentials([string(credentialsId: 'slack-e2e-webhook-url', variable: 'SLACK_WEBHOOK')]) {
                def slackBuildUrl = env.BUILD_URL ?: (env.RUN_DISPLAY_URL ?: "")
                withEnv(["SLACK_BUILD_URL=${slackBuildUrl}"]) {
                    sh '''#!/bin/bash
set -e
if [ -z "$SLACK_WEBHOOK" ]; then
  echo "SLACK_WEBHOOK not set; skipping Slack notification"
  exit 0
fi

# Read test summary if available
summary_file="$WORKSPACE/e2e_tests/test_summary.txt"
if [ -f "$summary_file" ]; then
  summary=$(cat "$summary_file")
else
  summary="summary not available; see console"
fi

cat > slack_payload.json <<EOF
{
  "blocks": [
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "❌ *E2E API Tests FAILED - ${JOB_NAME} #${BUILD_NUMBER}*"
      }
    },
    {
      "type": "section",
      "fields": [
        {
          "type": "mrkdwn",
          "text": "*Result:*\\n❌ FAILED"
        },
        {
          "type": "mrkdwn",
          "text": "*Jenkins Run:*\\n<${SLACK_BUILD_URL}|Open build>"
        }
      ]
    },
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "*Summary:*\\n${summary}"
      }
    }
  ]
}
EOF

curl -sS -X POST -H 'Content-type: application/json' \
     --data @slack_payload.json \
     "$SLACK_WEBHOOK" || echo "Slack notification failed (non-fatal)"
                    '''
                }
            }
        }
        unstable {
            echo "⚠️ E2E API tests completed with warnings"
        }
    }
}

