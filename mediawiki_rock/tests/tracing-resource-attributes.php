<?php
// Copyright 2026 Canonical Ltd.
// See LICENSE file for licensing details.

use GuzzleHttp\Client;
use GuzzleHttp\Handler\MockHandler;
use GuzzleHttp\HandlerStack;
use GuzzleHttp\Middleware;
use GuzzleHttp\Psr7\HttpFactory;
use GuzzleHttp\Psr7\Response;
use Psr\Log\NullLogger;
use Wikimedia\Telemetry\OtlpHttpExporter;
use Wikimedia\Telemetry\SpanContext;
use Wikimedia\Telemetry\TracerState;

// Run against the patched release tree during the rock build, without booting a wiki.
$root = $argv[1] ?? throw new InvalidArgumentException( 'Provide the MediaWiki source directory.' );
// Use the core autoloader that is available before LocalSettings.php runs.
require "$root/includes/AutoLoader.php";

function expectSame( mixed $expected, mixed $actual, string $message ): void {
	if ( $expected !== $actual ) {
		throw new RuntimeException( "$message\nExpected: " . var_export( $expected, true ) .
			"\nActual: " . var_export( $actual, true ) );
	}
}

/**
 * Capture the real exporter's OTLP JSON without sending traffic to a collector.
 * Passing null leaves existing defaults untouched; an empty array clears them.
 */
function exportTrace( ?array $resourceAttributes = null ): array {
	if ( $resourceAttributes !== null ) {
		OtlpHttpExporter::setResourceAttributes( $resourceAttributes );
	}
	$history = [];
	$handler = HandlerStack::create( new MockHandler( [ new Response( 200 ) ] ) );
	$handler->push( Middleware::history( $history ) );
	$exporter = new OtlpHttpExporter(
		new Client( [ 'handler' => $handler ] ),
		new HttpFactory(),
		new NullLogger(),
		'http://collector:4318/v1/traces',
		'mediawiki',
		'mediawiki-0'
	);
	// Reset after construction to verify the exporter retains its own snapshot.
	OtlpHttpExporter::setResourceAttributes( [] );
	$state = new TracerState();
	// A root and child share one resource; enrichment must not alter either span.
	foreach ( [ 'aaaaaaaaaaaaaaaa' => null, 'bbbbbbbbbbbbbbbb' => 'aaaaaaaaaaaaaaaa' ] as $id => $parent ) {
		$span = new SpanContext(
			'0123456789abcdef0123456789abcdef', $id, $parent, 'test span', true
		);
		$span->setStartEpochNanos( 100 );
		$span->setEndEpochNanos( 200 );
		$span->setAttributes( [ 'existing.attribute' => 'preserved' ] );
		$state->addSpanContext( $span );
	}
	$exporter->export( $state );
	expectSame( [], $state->getSpanContexts(), 'Finished spans must still be cleared after export.' );
	expectSame( 1, count( $history ), 'Export must make one HTTP request.' );
	$request = $history[0]['request'];
	expectSame( 'POST', $request->getMethod(), 'Export must still use POST.' );
	expectSame( 'http://collector:4318/v1/traces', (string)$request->getUri(), 'Preserve the endpoint.' );
	expectSame( 'application/json', $request->getHeaderLine( 'Content-Type' ), 'Preserve the content type.' );
	return json_decode( (string)$request->getBody(), true, 512, JSON_THROW_ON_ERROR );
}

// Index by key while retaining OTLP value wrappers so scalar types remain testable.
function attributes( array $payload ): array {
	$result = [];
	foreach ( $payload['resourceSpans'][0]['resource']['attributes'] as $attribute ) {
		$result[$attribute['key']] = $attribute['value'];
	}
	return $result;
}

$_SERVER['SERVER_ADDR'] = '192.0.2.10';
$builtIn = [
	'service.name' => [ 'stringValue' => 'mediawiki' ],
	'host.name' => [ 'stringValue' => 'mediawiki-0' ],
	'server.socket.address' => [ 'stringValue' => '192.0.2.10' ],
];
// Keep an unconfigured payload for resource and span compatibility comparisons.
$baseline = exportTrace();
expectSame( $builtIn, attributes( $baseline ), 'Not calling the setter must preserve existing resources.' );
expectSame( $baseline, exportTrace( [] ), 'An empty map must preserve the entire exported payload.' );

$topology = [
	'juju_model' => 'testing',
	'juju_model_uuid' => '6996ba60-70ba-4da0-8e89-2dbae173a46f',
	'juju_application' => 'mediawiki',
	'juju_unit' => 'mediawiki/0',
	'juju_charm' => 'mediawiki-k8s',
];
// False, zero and empty strings are valid attributes and must not be filtered out.
$additional = [
	'empty' => '',
	'zero' => 0,
	'disabled' => false,
	'ratio' => 0.25,
];
$payload = exportTrace( $topology + $additional );
$expected = [];
foreach ( $topology as $key => $value ) {
	$expected[$key] = [ 'stringValue' => $value ];
}
$expected += [
	'empty' => [ 'stringValue' => '' ],
	'zero' => [ 'intValue' => 0 ],
	'disabled' => [ 'boolValue' => false ],
	'ratio' => [ 'doubleValue' => 0.25 ],
];
expectSame( $expected + $builtIn, attributes( $payload ), 'Serialize topology and retain scalar types.' );
expectSame( 1, count( $payload['resourceSpans'] ), 'Both spans must share the enriched resource.' );
expectSame(
	$baseline['resourceSpans'][0]['scopeSpans'],
	$payload['resourceSpans'][0]['scopeSpans'],
	'Resource enrichment must preserve both root and child spans and their attributes.'
);

expectSame( $builtIn, attributes( exportTrace( [
	'service.name' => 'incorrect-service',
	'host.name' => 'incorrect-host',
	'server.socket.address' => '192.0.2.20',
] ) ), 'Built-in resource values must take precedence.' );

// Replacing defaults must not retain topology keys omitted by the next caller.
OtlpHttpExporter::setResourceAttributes( $topology );
OtlpHttpExporter::setResourceAttributes( [ 'juju_unit' => 'mediawiki/7' ] );
expectSame(
	[ 'juju_unit' => [ 'stringValue' => 'mediawiki/7' ] ] + $builtIn,
	attributes( exportTrace() ),
	'A setter call must replace, not merge, the defaults for new exporters.'
);

OtlpHttpExporter::setResourceAttributes( $topology );
OtlpHttpExporter::setResourceAttributes( [] );
expectSame( $baseline, exportTrace(), 'An empty map must reset defaults for new exporters.' );

// Reject invalid maps atomically: the last valid defaults must survive every failure.
OtlpHttpExporter::setResourceAttributes( [ 'juju_unit' => 'mediawiki/7' ] );
foreach ( [
	[ '' => 'empty key' ],
	[ 0 => 'numeric key' ],
	[ 'invalid' => null ],
	[ 'invalid' => [] ],
	[ 'invalid' => new stdClass() ],
	[ 'invalid' => INF ],
	[ 'invalid' => NAN ],
] as $invalid ) {
	try {
		exportTrace( $invalid );
	} catch ( InvalidArgumentException $e ) {
		continue;
	}
	throw new RuntimeException( 'Invalid resource attributes must fail explicitly.' );
}
expectSame(
	[ 'juju_unit' => [ 'stringValue' => 'mediawiki/7' ] ] + $builtIn,
	attributes( exportTrace() ),
	'Invalid setter calls must leave previously configured defaults unchanged.'
);

unset( $_SERVER['SERVER_ADDR'] );
expectSame(
	array_diff_key( $builtIn, [ 'server.socket.address' => true ] ),
	attributes( exportTrace() ),
	'An unavailable server address must still be omitted.'
);

echo "PASS: tracing resource attributes, snapshots, replacement, reset, compatibility and validation.\n";
