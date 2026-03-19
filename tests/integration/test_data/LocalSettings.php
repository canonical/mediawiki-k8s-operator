<?php
// Copyright 2026 Canonical Ltd.
// See LICENSE file for licensing details.

# Debugging settings
error_reporting( E_ALL );
ini_set( 'display_errors', 1 );

$wgShowExceptionDetails = true;

# ShortURL
$wgArticlePath = "/title/$1";

# Skin
$wgDefaultSkin = "vector-2022";
wfLoadSkin( 'Vector' );

$wgEnableUploads = true;

# Extensions
wfLoadExtension( 'CheckUser' );
wfLoadExtension( 'Linter' );
wfLoadExtension( 'VisualEditor' );
wfLoadExtension( 'DiscussionTools' );
wfLoadExtension( 'Echo' );
wfLoadExtension( 'Nuke' );
wfLoadExtension( 'Thanks' );
wfLoadExtension( 'WikiEditor' );
