<?php
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

require_once ( getenv( 'MEDIAWIKI_ROOT' ) ?: '/var/www/html/w' ) . '/maintenance/Maintenance.php';

class CheckS3Backend extends MediaWiki\Maintenance\Maintenance {
    public function execute() {
        $backend = $this->getServiceContainer()->getFileBackendGroup()->get( 'AmazonS3' );
        $method = new ReflectionMethod( AmazonS3FileBackend::class, 'getLocalCopyCached' );
        $lines = file( $method->getFileName() );
        $source = implode( '', array_slice( $lines, $method->getStartLine() - 1,
            $method->getEndLine() - $method->getStartLine() + 1 ) );
        $normalized = '';
        foreach ( token_get_all( "<?php\n" . $source ) as $token ) {
            if ( is_array( $token ) ) {
                if ( in_array( $token[0], [ T_OPEN_TAG, T_WHITESPACE, T_COMMENT, T_DOC_COMMENT ], true ) ) {
                    continue;
                }
                $normalized .= $token[1];
            } else {
                $normalized .= $token;
            }
        }
        $this->output( json_encode( [
            'backend' => get_class( $backend ),
            'upstream_sha256' => hash( 'sha256', $normalized ),
            'protected' => $method->isProtected(),
            'parameters' => $method->getNumberOfParameters(),
            'curl_version' => curl_version()['version_number'],
        ] ) . "\n" );
    }
}

$maintClass = CheckS3Backend::class;
require_once RUN_MAINTENANCE_IF_MAIN;
