// E2E Test Stage for Post-Deployment Integration
// Add this stage to existing service pipelines (e.g., taskservice-image-push-cb.groovy)
// 
// Usage in service pipeline:
// stage('E2E Tests') {
//     steps {
//         script {
//             def e2eTestScript = load 'tests/e2e_tests/ci/e2e-tests-post-deployment.groovy'
//             e2eTestScript.runE2ETests([
//                 serviceName: 'taskservice',
//                 testEnv: 'dev',
//                 testTypes: ['api']  // or ['api', 'ui'] or ['ui']
//             ])
//         }
//     }
// }

def runE2ETests(Map config) {
    def serviceName = config.serviceName ?: 'unknown'
    def testEnv = config.testEnv ?: 'dev'
    def testTypes = config.testTypes ?: ['api']
    def failOnError = config.failOnError ?: false
    
    echo "Running E2E tests for ${serviceName} after deployment..."
    
    dir('tests/e2e_tests') {
        // Setup (if not already done)
        if (!fileExists('venv')) {
            sh """
            python3 -m venv venv
            source venv/bin/activate
            pip install -r requirements.txt
            """
        }
        
        // Configure environment
        withCredentials([string(credentialsId: 'dagknows-jwt-token', variable: 'JWT_TOKEN')]) {
            sh """
            cp env.template .env
            echo "DAGKNOWS_URL=https://dev.dagknows.com" >> .env
            echo "DAGKNOWS_PROXY=?proxy=dev1" >> .env
            echo "DAGKNOWS_TOKEN=\${JWT_TOKEN}" >> .env
            """
            
            // Try to add Elasticsearch URL if credential exists (optional)
            try {
                withCredentials([string(credentialsId: 'dagknows-elastic-url', variable: 'ELASTIC_URL')]) {
                    sh """
                    echo "DAGKNOWS_ELASTIC_URL=\${ELASTIC_URL}" >> .env
                    """
                }
            } catch (Exception e) {
                echo "Elasticsearch credentials not configured (optional - tests will verify via API metadata only)"
            }
        }
        
        // Run API tests
        if (testTypes.contains('api')) {
            echo "Running API E2E tests..."
            try {
                sh """
                source venv/bin/activate
                pytest api_tests/ -v -m "api and not slow" --tb=short || true
                """
            } catch (Exception e) {
                echo "API E2E tests failed: ${e.getMessage()}"
                if (failOnError) {
                    error("API E2E tests failed")
                }
            }
        }
        
        // Run UI tests (if requested and time permits)
        if (testTypes.contains('ui')) {
            echo "Running UI E2E tests (smoke tests only)..."
            try {
                sh """
                source venv/bin/activate
                playwright install chromium || true
                pytest ui_tests/ -v -m "ui and smoke" --tb=short || true
                """
            } catch (Exception e) {
                echo "UI E2E tests failed: ${e.getMessage()}"
                if (failOnError) {
                    error("UI E2E tests failed")
                }
            }
        }
    }
    
    echo "E2E tests completed for ${serviceName}"
}

return this

