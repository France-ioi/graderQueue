<?php

# Copyright (c) 2015 France-IOI, MIT license
#
# http://opensource.org/licenses/MIT

require("config.inc.php");

list($platdata, $request) = get_token_client_info();

if ($platdata && $request) {
  # Client was identified
  $received_from = $platdata['id'];
} elseif ($CFG_accept_interface_tokens && isset($_POST['token'])) {
  $request = $_POST;
  # API used through interface.php, check token for validity
  $stmt = $db->prepare("SELECT * FROM `tokens` WHERE expiration_time >= NOW() AND token = :token;");
  $stmt->execute(array(':token' => $request['token']));
  if($stmt->fetch()) {
    $received_from = -1;
    $platdata = array(
        'restrict_paths' => '',
        'force_tag' => -1);
  } else {
    die(jsonerror(3, "Invalid token, please refresh the interface to get a new one."));
  }
  $db->query("DELETE FROM `tokens` WHERE expiration_time < NOW();");
} else {
  die(jsonerror(2, "No valid authentication provided."));
}

if(!isset($request['request'])) {
  die(jsonerror(2, "No request made."));

} elseif($request['request'] == 'sendjob' or $request['request'] == 'sendsolution') {
  # Send a job, either directly the JSON data, either the solution information

  if($request['request'] == 'sendjob') {
    # Client is sending job JSON directly
    if(!isset($request['jobdata'])) {
      die(jsonerror(2, "jobdata missing from request."));
    }
    try {
        $evaljson = json_decode($request['jobdata']);
    } catch(Exception $e) {
        die(jsonerror(2, "Error while decoding job JSON : " . $e->getMessage()));
    }

    if(isset($request['jobname'])) {
      $jobname = $request['jobname'];
    } else {
      $jobname = "api-sendjob";
    }

  } elseif($request['request'] == 'sendsolution') {
    # Client is sending only parameters, we make the job JSON from those
    foreach(array('taskpath', 'memlimit', 'timelimit', 'lang') as $key) {
      if(!isset($request[$key])) {
        die(jsonerror(2, $key . " missing from request."));
      }
    }

    if(!((isset($request['solpath']) and $request['solpath'] != '')
        or (isset($request['solcontent']) and $request['solcontent'] != '')
        or (isset($_FILES['solfile']) and is_uploaded_file($_FILES['solfile']['tmp_name'])))) {
      die(jsonerror(2, "Solution missing from request."));
    }

    if(!isset($request['lang'])) {
      die(jsonerror(2, "Solution language missing from request."));
    }
    $sollang = $request['lang'];

    $memlimit = max(4, intval($request['memlimit']));
    $timelimit = max(1, intval($request['timelimit']));
    $priority = max(0, intval($request['priority']));

    # Make JSON (as stdGrade from taskgrader does)
    $paramsjson = array();

    if(isset($request['solpath']) and $request['solpath'] != '') {
      $paramsjson['solutionFilename'] = basename($request['solpath']);
      $paramsjson['solutionPath'] = $request['solpath'];
    } elseif(isset($request['solcontent']) and $request['solcontent'] != '') {
      # Adapt to sol
      if(isset($CFG_defaultexts[$sollang])) {
        $paramsjson['solutionFilename'] = "main" . $CFG_defaultexts[$sollang];
      } else {
        $paramsjson['solutionFilename'] = "main" . $CFG_defaultexts['[default]'];
      }
      $paramsjson['solutionContent'] = $request['solcontent'];
    } else {
      $paramsjson['solutionFilename'] = $_FILES['solfile']['name'];
      $paramsjson['solutionContent'] = file_get_contents($_FILES['solfile']['tmp_name']);
    }

    if(isset($request['jobname'])) {
      $jobname = $request['jobname'];
    } else {
      $jobname = "api-" . $paramsjson['solutionFilename'];
    }

    $execparamsjson = array("timeLimitMs" => $timelimit,
        "memoryLimitKb" => $memlimit,
        "useCache" => True,
        "stdoutTruncateKb" => -1,
        "stderrTruncateKb" => -1,
        "getFiles" => array());

    $paramsjson = $paramsjson + array(
        "solutionId" => "sol-" . $paramsjson['solutionFilename'],
        "solutionExecId" => "exec-" . $paramsjson['solutionFilename'],
        "solutionLanguage" => $sollang,
        "solutionDependencies" => "@defaultDependencies-" . $sollang,
        "defaultSolutionCompParams" => $execparamsjson,
        "defaultSolutionExecParams" => $execparamsjson);

    $evaljson = array(
        "taskPath" => $request['taskpath'],
        "extraParams" => $paramsjson);
  }

  $priority = max(0, intval($request['priority']));

  # Convert tags to list of server types which can execute the job
  if(isset($_POST['tags'])) {
    $tagids = tags_to_tagids($request['tags']);
  } else {
    $tagids = array();
  }

  # Tasks sent by this platform have a tag automatically added    
  if($platdata['force_tag'] != -1) {
    $tagids[] = $platdata['force_tag'];
  }

  # Fetch all server types which can execute with these tags
  $typeids = tagids_to_typeids($tagids);
  if(count($typeids) == 0 && count($tagids) > 0) {
    die(jsonerror(2, "No server type can execute jobs with tags " . $request['tags'] . "."));
  }

  # Add path restrictions if needed
  if($platdata['restrict_paths'] != '') {
    $evaljson['restrictToPaths'] = $platdata['restrict_paths'];
  }

  # Insert into queue
  $db->query("START TRANSACTION;");

  # Queue entry
  $stmt = $db->prepare("INSERT INTO `queue` (name, priority, received_from, received_time, tags, jobdata) VALUES(:name, :priority, :recfrom, NOW(), :tags, :jobdata);");
  $jsondata = json_encode($evaljson);
  $stmt->execute(array(':name' => $jobname, ':priority' => $priority, ':recfrom' => $received_from, ':tags' => $request['tags'], ':jobdata' => $jsondata));

  $jobid = $db->lastInsertId();
  if(count($typeids) > 0) {
    # Only some server types can execute it
    $db->query("INSERT INTO `job_types` (jobid, typeid) VALUES (" . $jobid . "," . implode("), (" . $jobid . ",", $typeids) . ");");
  } else {
    # Set the job to be accepted by any server
    $db->query("INSERT INTO `job_types` (jobid, typeid) SELECT " . $jobid . ", server_types.id FROM server_types;");
  }

  $db->query("COMMIT;");

  echo json_encode(array('errorcode' => 0, 'errormsg' => "Queued as job ID #" . $jobid . ".", 'jobid' => $jobid));
  flush();

  # Wake up a server if needed
  wake_up_server_by_type($typeids);

} elseif($request['request'] == "getjob") {
  # Read job information
  if(!isset($request['jobid'])) {
    die(jsonerror(2, "No jobid given."));
  }
  $jobid = intval($request['jobid']);
  if($request['jobid'] != strval($jobid)) {
    die(jsonerror(2, "Invalid jobid."));
  }

  $stmt1 = $db->prepare("SELECT * FROM queue WHERE id = :id AND received_from = :recfrom;");
  $stmt2 = $db->prepare("SELECT * FROM done WHERE jobid = :id AND received_from = :recfrom;");
  $stmt1->execute(array(':id' => $jobid, ':recfrom' => $received_from));
  $stmt2->execute(array(':id' => $jobid, ':recfrom' => $received_from));
  if($row = $stmt1->fetch()) {
    echo json_encode(array('errorcode' => 0, 'errormsg' => 'Success', 'origin' => 'queue', 'data' => $row));
  } elseif($row = $stmt2->fetch()) {
    echo json_encode(array('errorcode' => 0, 'errormsg' => 'Success', 'origin' => 'done', 'data' => $row));
  } else {
    echo jsonerror(2, "Invalid jobid.");
  }

} elseif($request['request'] == "test") {
  # Test connection
  die(jsonerror(0, "Connected as platform id ".$platdata['id']));
} elseif($request['request'] == "wakeup" && $received_from == -1) {
  # Wake-up a server (only through interface)
  $sid = max(0, intval($request['serverid']));
  if(wake_up_server_by_id($sid)) {
    die(jsonerror(0, "Server wake-up successful."));
  } else {
    die(jsonerror(1, "Server wake-up failed."));
  }
} else {
  die(jsonerror(2, "No request made."));
}
