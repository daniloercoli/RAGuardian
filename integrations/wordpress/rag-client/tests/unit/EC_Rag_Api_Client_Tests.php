<?php
/**
 * EC_Rag_Api_Client_Tests
 *
 * @package Tests\Unit
 */

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\TestCase;

final class EC_Rag_Api_Client_Tests extends TestCase {

    protected function setUp(): void {
        $GLOBALS['ec_rag_test_transients'] = [];
        $GLOBALS['ec_rag_test_filters']['ec_rag_api_retry_delay'] = function () {
            return 0;
        };
        $GLOBALS['ec_rag_test_http_handler'] = function () {
            return [
                'response' => ['code' => 200],
                'body'     => '{}',
                'headers'  => [],
            ];
        };
    }

    protected function tearDown(): void {
        unset($GLOBALS['ec_rag_test_transients']);
        unset($GLOBALS['ec_rag_test_filters']['ec_rag_api_retry_delay']);
    }

    private function client(array $overrides = []): \EC_Rag_Api_Client {
        return new \EC_Rag_Api_Client(function () use ($overrides) {
            return array_merge([
                'base_url'        => 'https://rag.example.test',
                'api_key'         => 'test-key',
                'request_timeout' => '5',
            ], $overrides);
        });
    }

    public function test_post_decodes_successful_json_response(): void {
        $GLOBALS['ec_rag_test_http_handler'] = function ($method, $url, $args) {
            self::assertSame('POST', $method);
            self::assertSame('https://rag.example.test/api/v1/query', $url);
            self::assertSame('test-key', $args['headers']['X-API-Key']);
            self::assertJson($args['body']);

            return [
                'response' => ['code' => 200],
                'body'     => '{"answer":"ok"}',
                'headers'  => [],
            ];
        };

        self::assertSame(['answer' => 'ok'], $this->client()->post('/api/v1/query', ['query' => 'hello']));
    }

    public function test_retries_transient_server_errors(): void {
        $attempts = 0;
        $GLOBALS['ec_rag_test_http_handler'] = function () use (&$attempts) {
            $attempts++;

            if ($attempts === 1) {
                return [
                    'response' => ['code' => 503],
                    'body'     => '{"message":"try later"}',
                    'headers'  => [],
                ];
            }

            return [
                'response' => ['code' => 200],
                'body'     => '{"status":"healthy"}',
                'headers'  => [],
            ];
        };

        self::assertSame(['status' => 'healthy'], $this->client()->get('/api/v1/health'));
        self::assertSame(2, $attempts);
    }

    public function test_does_not_retry_client_errors(): void {
        $attempts = 0;
        $GLOBALS['ec_rag_test_http_handler'] = function () use (&$attempts) {
            $attempts++;

            return [
                'response' => ['code' => 403],
                'body'     => '{"message":"forbidden"}',
                'headers'  => [],
            ];
        };

        $result = $this->client()->get('/api/v1/health');

        self::assertInstanceOf(\WP_Error::class, $result);
        self::assertSame('forbidden', $result->get_error_message());
        self::assertSame(1, $attempts);
    }

    public function test_knowledge_base_catalog_uses_short_single_attempt_and_cache(): void {
        $attempts = 0;
        $GLOBALS['ec_rag_test_http_handler'] = function ($method, $url, $args) use (&$attempts) {
            $attempts++;
            self::assertSame('GET', $method);
            self::assertSame(
                'https://rag.example.test/api/v1/knowledge-bases',
                $url
            );
            self::assertSame(
                \EC_Rag_Api_Client::KNOWLEDGE_BASE_CATALOG_TIMEOUT,
                $args['timeout']
            );

            return [
                'response' => ['code' => 200],
                'body'     => '{"knowledge_bases":[{"id":"default","status":"active"}]}',
                'headers'  => [],
            ];
        };
        $client = $this->client(['request_timeout' => '120']);

        $first = $client->get_knowledge_base_catalog();
        $second = $client->get_knowledge_base_catalog();

        self::assertSame($first, $second);
        self::assertSame('default', $second['knowledge_bases'][0]['id']);
        self::assertSame(1, $attempts);
    }

    public function test_knowledge_base_catalog_does_not_retry_and_caches_failures(): void {
        $attempts = 0;
        $GLOBALS['ec_rag_test_http_handler'] = function () use (&$attempts) {
            $attempts++;

            return [
                'response' => ['code' => 503],
                'body'     => '{"message":"temporarily unavailable"}',
                'headers'  => [],
            ];
        };
        $client = $this->client();

        $first = $client->get_knowledge_base_catalog();
        $second = $client->get_knowledge_base_catalog();

        self::assertInstanceOf(\WP_Error::class, $first);
        self::assertInstanceOf(\WP_Error::class, $second);
        self::assertSame(1, $attempts);
    }

    public function test_configured_knowledge_base_is_injected_server_side(): void {
        $knowledge_base_id = 'kb_11111111111111111111111111111111';
        $requests = [];
        $GLOBALS['ec_rag_test_http_handler'] = function ($method, $url, $args) use (&$requests) {
            $requests[] = [$method, $url, $args];
            return [
                'response' => ['code' => 200],
                'body'     => '{}',
                'headers'  => [],
            ];
        };
        $client = $this->client(['knowledge_base_id' => $knowledge_base_id]);

        $client->post(
            '/api/v1/query',
            [
                'query' => 'hello',
                'knowledge_base_id' => 'kb_22222222222222222222222222222222',
            ]
        );
        $client->post_multipart(
            '/api/v1/files?async=true',
            ['relative_path' => 'wordpress/post.txt'],
            [
                'filename' => 'post.txt',
                'content_type' => 'text/plain',
                'content' => 'content',
            ]
        );
        $client->get('/api/v1/health');
        $client->delete('/api/v1/files/wordpress/post.txt');

        self::assertSame(
            $knowledge_base_id,
            json_decode($requests[0][2]['body'], true)['knowledge_base_id']
        );
        self::assertStringContainsString(
            'name="knowledge_base_id"' . "\r\n\r\n" . $knowledge_base_id,
            $requests[1][2]['body']
        );
        self::assertStringEndsWith(
            '?knowledge_base_id=' . $knowledge_base_id,
            $requests[2][1]
        );
        self::assertStringEndsWith(
            '?knowledge_base_id=' . $knowledge_base_id,
            $requests[3][1]
        );
    }
}
