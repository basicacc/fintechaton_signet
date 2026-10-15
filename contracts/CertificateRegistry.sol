// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.20;

/// @title CertificateRegistry
/// @notice Tamper-evident anchor for compliance certificates.
///
/// Institutions anchor the Merkle root of a batch of certificate digests; a single
/// transaction covers thousands of certificates. Only digests go on-chain, never
/// personal data. Regulators run their own node in the consortium and can flag a
/// certificate on-chain, which every participant can see.
///
/// Leaves are keccak256(certificate digest). Internal nodes hash the sorted pair,
/// so a proof is just the list of sibling hashes.
contract CertificateRegistry {
    struct Batch {
        bytes32 root;
        uint64 count;
        uint64 timestamp;
        address anchoredBy;
    }

    address public owner;
    mapping(address => bool) public isAnchorer;
    mapping(address => bool) public isRegulator;

    Batch[] private _batches;
    mapping(bytes32 => uint256) public batchIndexPlusOne; // root -> index + 1
    mapping(bytes32 => bytes32) public flagOf; // certificate digest -> reason hash

    event AnchorerSet(address indexed account, bool allowed);
    event RegulatorSet(address indexed account, bool allowed);
    event BatchAnchored(uint256 indexed index, bytes32 indexed root, uint64 count, address indexed by);
    event CertificateFlagged(bytes32 indexed digest, bytes32 reason, address indexed by);

    error NotOwner();
    error NotAnchorer();
    error NotRegulator();
    error EmptyBatch();
    error DuplicateRoot();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor() {
        owner = msg.sender;
        isAnchorer[msg.sender] = true;
        emit AnchorerSet(msg.sender, true);
    }

    function setAnchorer(address account, bool allowed) external onlyOwner {
        isAnchorer[account] = allowed;
        emit AnchorerSet(account, allowed);
    }

    function setRegulator(address account, bool allowed) external onlyOwner {
        isRegulator[account] = allowed;
        emit RegulatorSet(account, allowed);
    }

    function anchorBatch(bytes32 root, uint64 count) external returns (uint256 index) {
        if (!isAnchorer[msg.sender]) revert NotAnchorer();
        if (count == 0) revert EmptyBatch();
        if (batchIndexPlusOne[root] != 0) revert DuplicateRoot();
        index = _batches.length;
        _batches.push(Batch(root, count, uint64(block.timestamp), msg.sender));
        batchIndexPlusOne[root] = index + 1;
        emit BatchAnchored(index, root, count, msg.sender);
    }

    function flag(bytes32 digest, bytes32 reason) external {
        if (!isRegulator[msg.sender]) revert NotRegulator();
        flagOf[digest] = reason;
        emit CertificateFlagged(digest, reason, msg.sender);
    }

    function batchCount() external view returns (uint256) {
        return _batches.length;
    }

    function batch(uint256 index) external view returns (Batch memory) {
        return _batches[index];
    }

    /// @notice True if `digest` is included in the anchored batch with Merkle root `root`.
    function isAnchored(bytes32 digest, bytes32[] calldata proof, bytes32 root) external view returns (bool) {
        if (batchIndexPlusOne[root] == 0) return false;
        bytes32 h = keccak256(abi.encodePacked(digest));
        for (uint256 i = 0; i < proof.length; i++) {
            bytes32 p = proof[i];
            h = h < p ? keccak256(abi.encodePacked(h, p)) : keccak256(abi.encodePacked(p, h));
        }
        return h == root;
    }
}
